"""Read physical (or per-process) memory through the camera's existing root context.

`/dev/mem` is unrestricted on this camera (`CONFIG_DEVMEM=y`, no `STRICT_DEVMEM`), so
the root the preload gives us already reaches regions that were previously assumed to
need hardware access — the BootROM at 0xFFFF0000, SoC control registers, any process's
memory. This module performs **reads only**: it never writes to the device.

The camera has no base64, hexdump or split, and its shell captures at most 256 KiB per
command, so the region is written to a file on the device and pulled back in chunks
with `dd` — the one tool that exists on both sides.

`dd bs=` must be page-aligned for physical memory, so an aligned offset is read with
bs=4096 (fast) and an unaligned one with bs=1 (correct, slower).
"""
import json
import re
from pathlib import Path

from jiritsu_preload_deploy import READ_ATTEMPTS, submit_command
from jiritsu_run_command import hand_back
from jiritsu_space import fits, listed_bytes, partition_size_bytes, space_report

PAGE = 4096
DEFAULT_CHUNK = 64 * 1024
CARD_DEVICE = "/dev/mmca2"
# /tmp is a 12 MiB vfat partition shared with /etc and /var. Filling it
# breaks unrelated writes — including the one that neutralises
# /etc/ld.so.preload, which left the preload armed on the device.
TMP_BUDGET = 256 * 1024
CARD_POINT = "/tmp/sd"
# Fall back to the camera's Work partition (nflasha6, 25 MiB, ~20 MiB free), not to
# /tmp: /tmp shares a 12 MiB partition with /etc and /var, which is the one that got
# filled and then rejected every write. Requests here are capped by TMP_BUDGET and
# the file is removed after it is retrieved.
FALLBACK_DIR = "/tmp/jiritsu"
SIZE_PATTERN = re.compile(r"\s(\d+)\s")


def parse_number(text):
    value = int(str(text).strip(), 0)
    if value < 0:
        raise ValueError(f"must not be negative: {text!r}")
    return value


def parse_spec(text):
    """Parse `SOURCE:ID:OFFSET:SIZE[:NAME]`.

    SOURCE is `phys` (physical address, read through /dev/mem) or `pid` (virtual
    address inside that process, read through /proc/<pid>/mem). OFFSET and SIZE accept
    hex (0x…) or decimal notation.
    """
    parts = str(text).split(":")
    if len(parts) < 4:
        raise ValueError(f"expected SOURCE:ID:OFFSET:SIZE[:NAME], got {text!r}")
    source, ident, offset, size = parts[0].lower(), parts[1], parts[2], parts[3]
    # The length belongs in the default name: two regions sharing a base address but
    # different sizes would otherwise overwrite each other's file.
    name = (parts[4] if len(parts) > 4 and parts[4]
            else f"{source}-{ident}-{offset}-{size}.bin")
    if source not in ("phys", "pid"):
        raise ValueError(f"source must be phys or pid, got {source!r}")
    if source == "pid" and not str(ident).isdigit():
        raise ValueError(f"pid must be numeric, got {ident!r}")
    if not re.fullmatch(r"[0-9A-Za-z_.-]+", name):
        raise ValueError(f"unsafe output name: {name!r}")
    size_value = parse_number(size)
    if size_value == 0:
        raise ValueError("size must be greater than zero")
    return {"source": source, "id": ident, "offset": parse_number(offset),
            "size": size_value, "name": name}


def device_path(spec):
    if spec["source"] == "phys":
        return "/dev/mem"
    if not str(spec["id"]).isdigit():
        raise ValueError(f"pid must be numeric, got {spec['id']!r}")
    return f"/proc/{spec['id']}/mem"


def dd_read(device, offset, size, out_path):
    """Build the `dd` that copies one region out of a device into a file."""
    if offset % PAGE == 0:
        blocks = offset // PAGE
        count = (size + PAGE - 1) // PAGE
        return (f"rm -f {out_path}; "
                f"dd if={device} of={out_path} bs={PAGE} skip={blocks} count={count} "
                f"2>&1; echo DD_RC=$?; ls -l {out_path} 2>&1")
    return (f"rm -f {out_path}; "
            f"dd if={device} of={out_path} bs=1 skip={offset} count={size} "
            f"2>&1; echo DD_RC=$?; ls -l {out_path} 2>&1")


def dd_chunk(path, index, chunk, count):
    return f"dd if={path} bs={chunk} skip={index} count={count} 2>/dev/null"


MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def parse_file_size(listing):
    """Read the byte count out of an `ls -l` line.

    `ls -l` prints `perm links user group size Mon day time name`, so the size is the
    field immediately before the month — a stable position, unlike guessing from digit
    counts (the group column is often `0`, which broke an earlier heuristic).
    """
    for line in listing.splitlines():
        fields = line.split()
        for index, field in enumerate(fields):
            if field in MONTHS and index >= 1:
                candidate = fields[index - 1]
                if candidate.isdigit():
                    return int(candidate)
    return None


def memread_driver(specs, chunk=DEFAULT_CHUNK, attempts=READ_ATTEMPTS, save=None,
                   commands=None, card_only=False):
    """Build a deploy driver that reads each region and returns it to the host."""
    specs = [dict(item) for item in specs] if not isinstance(specs, dict) else [dict(specs)]
    if not specs:
        raise ValueError("at least one region is required")

    def driver(transport, requests, log, record):
        mounted = False
        probe = submit_command(transport, requests,
                               f"[ -b {CARD_DEVICE} ] && mkdir -p {CARD_POINT} && "
                               f"mount -t ext2 {CARD_DEVICE} {CARD_POINT} 2>&1; "
                               f"echo MOUNT_RC=$?; ls -d {CARD_POINT} 2>&1",
                               "card", log, attempts=attempts)
        if b"MOUNT_RC=0" in probe:
            mounted = True
            log("memread_card_mounted", point=CARD_POINT)
        else:
            log("memread_card_unavailable", note="using /tmp for the device-side file")
        directory = CARD_POINT if mounted else FALLBACK_DIR
        if mounted:
            # Ask before writing: the camera has no `df`, so free space comes from the
            # partition size (`/proc/partitions`) minus what is on the mount. If either
            # cannot be read the answer is "unknown", and unknown means do not write.
            partitions = submit_command(transport, requests, "cat /proc/partitions; true",
                                        "parts", log, attempts=attempts)
            listing = submit_command(transport, requests,
                                     f"busybox ls -l {CARD_POINT} 2>&1; true",
                                     "space", log, attempts=attempts)
            total = partition_size_bytes(partitions, CARD_DEVICE.rsplit("/", 1)[1])
            used = listed_bytes(listing.decode("ascii", "replace"))
            report = space_report(total, used)
            requested = sum(spec["size"] for spec in specs)
            log("space_preflight", requested=requested, **report)
            verdict = fits(requested, report)
            if verdict is not True:
                raise ValueError(
                    f"refusing to write {requested} bytes: "
                    + (report["reason"] or f"only {report['free']} bytes free on {CARD_POINT}"))
        if not mounted:
            requested = sum(spec["size"] for spec in specs)
            if requested > TMP_BUDGET:
                raise ValueError(
                    f"the card is not mounted, and {requested} bytes must not be written to "
                    f"/tmp ({TMP_BUDGET} byte budget): that partition also holds /etc, so "
                    "filling it breaks the cleanup. Insert the card and retry.")

        results = []
        for spec in specs:
            device = device_path(spec)
            out_path = f"{directory}/{spec['name']}"
            listing = submit_command(transport, requests, dd_read(device, spec["offset"],
                                                                  spec["size"], out_path),
                                     "dd", log, attempts=attempts)
            text = listing.decode("ascii", "replace")
            if "DD_RC=0" not in text:
                # One unreadable region must not cancel the others: /dev/mem refuses
                # non-DRAM addresses, and a session costs a power cycle to repeat.
                reason = next((line.strip() for line in text.splitlines()
                               if line.strip().startswith(("dd:", "bad "))), text[-120:].strip())
                # A partial file must not be left behind: on a small partition it can
                # fill the filesystem and break later writes.
                submit_command(transport, requests, f"rm -f {out_path}", "rm", log,
                               attempts=attempts)
                log("memread_region_failed", device=device, offset=hex(spec["offset"]),
                    reason=reason)
                results.append({"device": device, "offset": spec["offset"], "name": spec["name"],
                                "error": reason})
                continue
            on_device = parse_file_size(text)
            log("memread_written", device=device, path=out_path, bytes=on_device)
            if on_device is None:
                raise ValueError(f"cannot read the size of {out_path} from: {text[-200:]!r}")
            if on_device < spec["size"]:
                # Fewer bytes than asked for means the range runs past readable memory
                # (or past the process's mapping); record why instead of returning a
                # silently truncated region.
                log("memread_region_short", device=device, offset=hex(spec["offset"]),
                    requested=spec["size"], on_device=on_device)
                results.append({"device": device, "offset": spec["offset"], "name": spec["name"],
                                "error": f"device returned {on_device} bytes, "
                                         f"requested {spec['size']}"})
                continue

            if card_only:
                # A whole-RAM dump is ~229 MiB on this camera; pulling that through the
                # mailbox would take thousands of round trips, so the file is left on the
                # card and fetched by moving the card.
                results.append({"device": device, "offset": spec["offset"], "bytes": on_device,
                                "name": spec["name"], "on_device_path": out_path,
                                "card_only": True})
                log("memread_left_on_card", path=out_path, bytes=on_device)
                continue

            blocks = (on_device + chunk - 1) // chunk
            parts = []
            for index in range(blocks):
                raw = submit_command(transport, requests, dd_chunk(out_path, index, chunk, 1),
                                     f"c{index:02d}", log, attempts=attempts)
                body = parse_chunk(raw, f"c{index:02d}")
                if body:
                    parts.append(body)
            data = b"".join(parts)
            if len(data) < spec["size"]:
                raise ValueError(f"retrieved {len(data)} bytes but {spec['size']} were "
                                 f"requested; refusing to present a partial read")
            # A page-aligned dd reads whole blocks, so the device-side file can be up to
            # one page longer than the requested region; keep exactly what was asked for.
            data = data[:spec["size"]]
            if not mounted:
                # Leave nothing behind on the camera's small partition.
                submit_command(transport, requests, f"rm -f {out_path}", "clean", log,
                               attempts=attempts)
            results.append({"device": device, "offset": spec["offset"], "bytes": len(data),
                            "name": spec["name"], "on_device_path": out_path,
                            "preview_hex": data[:64].hex()})
            log("memread_retrieved", name=spec["name"], bytes=len(data),
                sha256=__import__("hashlib").sha256(data).hexdigest())
            if save:
                # Several regions in one session each need their own file; the given
                # path becomes a prefix so a single power cycle can read them all.
                target = Path(save)
                if len(specs) > 1:
                    target = target.with_name(f"{target.name}.{spec['name']}")
                target.write_bytes(data)
                hand_back(target)
                log("memread_saved", path=str(target))

        for index, command in enumerate(commands or (), start=1):
            from jiritsu_run_command import parse_reply
            raw = submit_command(transport, requests, command, f"x{index:02d}", log,
                                 attempts=attempts)
            body, status, error = parse_reply(raw, f"x{index:02d}")
            if error:
                log("memread_command_failed", command=command[:80], reason=error)
                results.append({"command": command, "error": error})
                continue
            log("memread_command_output", command=command[:80], bytes=len(body or ""))
            results.append({"command": command, "exit_status": status, "output": body})
        record["memreads"] = results
        record["payload_ran"] = True
        record["mailbox_ok"] = True
        return json.dumps(results, indent=2, ensure_ascii=False).encode()

    return driver


def parse_chunk(raw, marker):
    """Strip the worker envelope from a chunk reply and return its payload bytes."""
    text = raw if isinstance(raw, (bytes, bytearray)) else str(raw).encode()
    token = f"A7C2-{marker}\n".encode()
    start = text.find(token)
    if start < 0:
        return b""
    start += len(token)
    end = text.rfind(b"\nEXIT ")
    return text[start:end if end > start else len(text)]
