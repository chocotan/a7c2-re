#!/usr/bin/env python3
"""Inventory dumped partition images: filesystem type, contents, and which
reference subtree each one came from.

The reference tree only mirrors some of the camera's partitions, and several
partitions are never mounted at runtime, so a partition's identity is established
by content rather than by assumption: every file in an image is hashed and looked
up in the reference index, and the reference prefixes that account for the image's
files are reported. A partition whose files all match under one prefix is that
partition; one whose files match nothing is a partition the reference does not
contain (a boot image, a backup area, runtime state).

`blkid` identifies the filesystem; images with no filesystem are reported as raw
blobs with their size and first bytes so they are not silently ignored.
"""
import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path


def digest(path, chunk=1 << 20):
    hasher = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while block := handle.read(chunk):
            hasher.update(block)
    return hasher.hexdigest()


def reference_index(root):
    """Map (size, digest) -> sortable list of reference paths."""
    index = defaultdict(list)
    for path in Path(root).rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        index[(path.stat().st_size, digest(path))].append(
            "/" + str(path.relative_to(root)))
    return {key: sorted(value) for key, value in index.items()}


def filesystem_of(image):
    """Identify the filesystem on an image without mounting it."""
    facts = {"type": None, "label": None}
    if shutil.which("blkid"):
        proc = subprocess.run(["blkid", "-p", "-o", "export", str(image)],
                              capture_output=True, text=True)
        for line in proc.stdout.splitlines():
            key, _, value = line.partition("=")
            if key == "TYPE":
                facts["type"] = value.strip()
            elif key in ("LABEL", "LABEL_FATBOOT"):
                facts["label"] = value.strip()
    return facts


def walk_files(root):
    for path in Path(root).rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        yield "/" + str(path.relative_to(root)), path


def prefix_of(reference_path, depth=2):
    """Group a reference path by its top directories: /usr/share/x -> /usr/share."""
    parts = reference_path.strip("/").split("/")
    return "/" + "/".join(parts[:depth - 1]) if len(parts) > 1 else "/"


def mount_and_match(image, index):
    """Mount an image read-only and match its files against the reference index."""
    mountpoint = tempfile.mkdtemp(prefix="a7c2-part-")
    try:
        mounted = subprocess.run(["mount", "-o", "loop,ro", str(image), mountpoint],
                                 capture_output=True, text=True)
        if mounted.returncode != 0:
            return {"mounted": False,
                    "reason": (mounted.stderr or mounted.stdout).strip()[:160],
                    "head": Path(image).read_bytes()[:32].hex()}
        names, total = [], 0
        matches = Counter()
        matched_files, unmatched = 0, []
        for name, path in walk_files(mountpoint):
            names.append(name)
            total += path.stat().st_size
            found = index.get((path.stat().st_size, digest(path)))
            if found:
                matched_files += 1
                for reference_path in found:
                    matches[prefix_of(reference_path)] += 1
            else:
                unmatched.append((name, path.stat().st_size))
        return {"mounted": True, "files": len(names), "content_bytes": total,
                "top_level": sorted({name.split("/")[1] for name in names}),
                "sample": sorted(names)[:12],
                "matched_files": matched_files,
                "reference_prefixes": matches.most_common(6),
                "unmatched": {"count": len(unmatched), "sample": unmatched[:8]}}
    finally:
        if subprocess.run(["findmnt", "-rn", mountpoint], capture_output=True).stdout.strip():
            subprocess.run(["umount", mountpoint], check=False)
        Path(mountpoint).rmdir()


def inventory(image, index):
    result = {"image": str(image), "bytes": image.stat().st_size,
              "image_sha256": digest(image), "filesystem": filesystem_of(image)}
    result.update(mount_and_match(image, index))
    return result


def report(result, limit=8):
    name = Path(result["image"]).name
    fs = result["filesystem"]
    print(f"=== {name}  镜像 {result['bytes']:,} B  "
          f"类型={fs['type'] or '（无文件系统）'}  卷标={fs['label'] or '-'}")
    if not result.get("mounted"):
        print(f"    未挂载: {result.get('reason', '?')}")
        print(f"    开头字节: {result.get('head', '')}")
        print()
        return
    print(f"    文件 {result['files']} 个，内容 {result['content_bytes']:,} B；顶层: "
          f"{' '.join(result['top_level'][:12])}")
    print(f"    可在参照树中认领的文件: {result['matched_files']}/{result['files']}")
    for prefix, count in result["reference_prefixes"][:limit]:
        print(f"      {prefix:<28} {count} 个匹配")
    if result["unmatched"]["count"]:
        block = result["unmatched"]
        print(f"    未匹配 {block['count']} 个，例: "
              f"{[n for n, _ in block['sample'][:5]]}")
    print()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("images", nargs="+", metavar="IMAGE")
    parser.add_argument("--reference", metavar="DIR", help="Reference tree used for content matching")
    parser.add_argument("--out", metavar="FILE", help="Write the inventory as JSON")
    args = parser.parse_args(argv)

    index = reference_index(args.reference) if args.reference else {}
    if args.reference:
        print(f"参照树索引: {sum(len(v) for v in index.values())} 个文件\n")
    results = []
    for name in args.images:
        image = Path(name)
        if not image.exists():
            print(f"=== {image.name}\n    不存在\n")
            continue
        result = inventory(image, index)
        results.append(result)
        report(result)
    if args.out:
        Path(args.out).write_text(json.dumps(results, indent=2) + "\n")
        print(f"清单已写入 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
