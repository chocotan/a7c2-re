#!/usr/bin/env python3
"""Verify a partitioned system dump against a reference tree.

Each `--image IMAGE:PREFIX` is mounted read-only through a loop device and every
regular file under it is compared, byte for byte, with `PREFIX/<path>` in the
reference tree. `/` and `/usr` live on different partitions, so a dump is verified
as several images sharing one reference: the rootfs image contributes `/X`, and the
/usr image contributes `/usr/X`.

An earlier version extracted the trees with `debugfs rdump`, which silently skipped
whole directories (it produced none of /usr/lib's 299 files), so the comparison
looked complete while covering barely half the files. Mounting the image and hashing
in place removes that failure mode; mounting needs privileges, and the images are
opened read-only.
"""
import argparse
import contextlib
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path


def digest(path, chunk=1 << 20):
    hasher = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while block := handle.read(chunk):
            hasher.update(block)
    return hasher.hexdigest()


def file_index(root):
    """Map every regular file under `root` to its path, skipping symlinks."""
    index = {}
    for path in Path(root).rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        index["/" + str(path.relative_to(root))] = path
    return index


def parse_image_spec(text):
    """Parse `IMAGE:PREFIX`; the prefix defaults to the image root."""
    image, separator, prefix = text.rpartition(":")
    if not separator:
        return Path(text), ""
    return Path(image), "" if prefix in ("", "/") else "/" + prefix.strip("/")


def group_by_top(names):
    counts = Counter()
    for name in names:
        parts = name.split("/")
        counts["/" + (parts[1] if len(parts) > 1 else "")] += 1
    return dict(sorted(counts.items(), key=lambda item: -item[1]))


@contextlib.contextmanager
def mounted(image):
    """Mount an image read-only through a loop device."""
    if shutil.which("mount") is None:
        raise SystemExit("mount is required")
    mountpoint = tempfile.mkdtemp(prefix="a7c2-verify-")
    subprocess.run(["mount", "-o", "loop,ro", str(image), mountpoint], check=True)
    try:
        yield Path(mountpoint)
    finally:
        subprocess.run(["umount", mountpoint], check=True)
        Path(mountpoint).rmdir()


def compare(image_root, reference_root, prefix="", limit=20, allowed=()):
    """Compare one image's tree with the reference, reporting the virtual paths."""
    image_root, reference_root = Path(image_root), Path(reference_root)

    def virtual(path):
        return f"{prefix}{path}" if prefix else path

    image = file_index(image_root)
    reference = {}
    for name, path in file_index(reference_root).items():
        reference[name] = path
    shared, differing, identical = [], [], 0
    allowed_differences = []
    for name in sorted(image):
        target = virtual(name)
        if target not in reference:
            shared.append(name)
            continue
        if digest(image[name]) == digest(reference[target]):
            identical += 1
        elif target in allowed:
            allowed_differences.append({"path": target,
                                        "reason": "declared expected difference"})
        else:
            differing.append({"path": target, "dump_bytes": image[name].stat().st_size,
                              "reference_bytes": reference[target].stat().st_size})
    only_reference = sorted(set(reference) - {virtual(name) for name in image})
    result = {
        "image": str(image_root),
        "prefix": prefix or "/",
        "dumped_files": len(image),
        "reference_files": len(reference),
        "compared": identical + len(differing),
        "identical": identical,
        "differing_count": len(differing),
        "differing": differing[:limit],
        "allowed_differences": allowed_differences,
        "not_in_reference": {"count": len(shared), "by_top_directory": group_by_top(shared),
                             "sample": sorted(shared)[:limit]},
        "only_in_reference": {"count": len(only_reference),
                              "by_top_directory": group_by_top(only_reference),
                              "sample": only_reference[:limit]},
    }
    result["faithful"] = not differing
    return result


def report(result, limit=20):
    print(f"=== {result['image']}  前缀 {result['prefix']} ===")
    print(f"  镜像文件数        : {result['dumped_files']}")
    print(f"  与参照树比对      : {result['compared']}")
    print(f"    逐字节一致      : {result['identical']}")
    print(f"    不一致          : {result['differing_count']}")
    for item in result.get("allowed_differences", []):
        print(f"    已知差异（已声明，不计入不一致）: {item['path']}")
    for item in result["differing"][:limit]:
        print(f"      DIFF {item['path']}: {item['dump_bytes']:,} B vs "
              f"{item['reference_bytes']:,} B")
    for key, label in (("not_in_reference", "镜像有而参照无"), ("only_in_reference", "参照有而镜像无")):
        block = result[key]
        print(f"  {label}: {block['count']} 个")
        for name, count in list(block["by_top_directory"].items())[:6]:
            print(f"      {name:<26} {count}")
    print(f"  → {'✓ 共同文件全部逐字节一致' if result['faithful'] else '✗ 存在不一致文件'}")
    print()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--image", action="append", required=True, metavar="IMAGE[:PREFIX]",
                        help="Partition image and the reference prefix it maps to, repeatable")
    parser.add_argument("--reference", required=True, metavar="DIR", help="Reference tree directory")
    parser.add_argument("--limit", type=int, default=20, help="Report at most N examples per set")
    parser.add_argument("--allow-difference", action="append", default=[], metavar="PATH",
                        help="A path whose difference is expected and already explained; it is "
                             "still reported, but does not make the verification fail")
    parser.add_argument("--out", metavar="FILE", help="Write all results as JSON")
    args = parser.parse_args(argv)

    results = []
    allowed = tuple(args.allow_difference)
    for spec in args.image:
        image, prefix = parse_image_spec(spec)
        if not image.exists():
            raise SystemExit(f"no such image: {image}")
        with mounted(image) as root:
            results.append(compare(root, args.reference, prefix, args.limit, allowed))
    for result in results:
        report(result, args.limit)

    compared = sum(r["compared"] for r in results)
    identical = sum(r["identical"] for r in results)
    differing = sum(r["differing_count"] for r in results)
    print(f"合计：比对 {compared} 个文件，逐字节一致 {identical}，不一致 {differing}")
    if args.out:
        Path(args.out).write_text(json.dumps(results, indent=2) + "\n")
        print(f"结果已写入 {args.out}")
    return 0 if all(r["faithful"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
