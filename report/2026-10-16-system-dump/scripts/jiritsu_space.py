"""Report free space on the camera's target partition before a bulk write.

The camera has no `df` (verified: `df: not found`) and no shell-visible statvfs, so free
space is derived from two facts we *can* observe:

* the partition's size, from `/proc/partitions` (a file the read path can fetch), and
* what is already on it — for the card's ext2 partition that is exactly what our own
  tools wrote, because the camera's media stack only understands the exFAT partition and
  never touches the ext2 one.

Sizes are parsed rather than trusted: a pre-flight that cannot read the partition size,
or whose listing cannot be parsed, reports "unknown" instead of guessing a number that
would then authorise a write.
"""
import re

SIZE_LINE = re.compile(r"^\s*\d+\s+\d+\s+(\d+)\s+(\S+)\s*$")


def partition_size_bytes(proc_partitions, device_name):
    """Return the size of a partition in bytes, or None when it is not listed."""
    for line in proc_partitions.decode("ascii", "replace").splitlines():
        match = SIZE_LINE.match(line)
        if match and match.group(2) == device_name:
            return int(match.group(1)) * 1024          # /proc/partitions reports KiB
    return None


MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def listed_bytes(listing):
    """Sum the sizes in an `ls -l` listing; None when nothing parses.

    `ls -l` prints `perm links user group size Mon day time name`, so the size is the
    field immediately before the month — taking the first number instead would read the
    hard-link count (always 1) and under-count by three orders of magnitude, which would
    authorise a write that does not fit. Entries whose size cannot be read make the whole
    result unknown rather than a low estimate.
    """
    if any(marker in listing for marker in ("ls:", "No such file", "Permission denied")):
        return None                      # the command failed; do not read that as "empty"
    total = 0
    seen = 0
    for line in listing.splitlines():
        fields = line.split()
        if not fields or not fields[0].startswith("-"):
            continue
        for index, field in enumerate(fields):
            if field in MONTHS and index >= 1 and fields[index - 1].isdigit():
                total += int(fields[index - 1])
                seen += 1
                break
    return total          # no entries and no error text means an empty directory = 0


def space_report(partition_size, used_bytes):
    """Turn the two observations into a report, keeping 'unknown' explicit."""
    if partition_size is None or used_bytes is None:
        return {"partition_size": partition_size, "used": used_bytes, "free": None,
                "sufficient": None, "reason": "size or usage could not be read"}
    free = partition_size - used_bytes
    return {"partition_size": partition_size, "used": used_bytes, "free": free,
            "sufficient": free > 0, "reason": None}


DEFAULT_MARGIN = 1 << 20          # 1 MiB of headroom; "exactly fits" is not safe


def fits(request_bytes, report, margin=DEFAULT_MARGIN):
    """Whether a write of `request_bytes` fits with headroom.

    Returns None when the report is unknown, so callers must treat "cannot tell" as
    "do not proceed" rather than as permission.
    """
    free = report.get("free")
    if free is None:
        return None
    return request_bytes + margin < free
