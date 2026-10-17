#!/usr/bin/env python3
"""Copy dumped images off the card and verify every copy against its source.

The card is mounted read-only by the caller and is never written here. Each image is
copied and then hashed on both sides: a size match alone is not evidence that the
bytes arrived intact, and a truncated copy is exactly the failure this is meant to
catch before the card is reused.

Usage:
    python3 copy_card_images.py /mnt/a7c2-fetch analysis/output/system-dump
"""
import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

CHUNK = 1 << 20
EXTRA_ARTIFACTS = ("LISTING", "DONE")


def digest(path):
    hasher = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while block := handle.read(CHUNK):
            hasher.update(block)
    return hasher.hexdigest()


def copy_verified(source, destination):
    """Copy one file, then confirm the copy carries the same bytes."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_size = source.stat().st_size
    if destination.exists() and destination.stat().st_size == source_size:
        if digest(destination) == digest(source):
            return {"file": source.name, "bytes": source_size, "sha256": digest(source),
                    "action": "skipped_identical"}
    shutil.copyfile(source, destination)
    source_digest, copy_digest = digest(source), digest(destination)
    if source_digest != copy_digest:
        return {"file": source.name, "bytes": source_size, "action": "failed",
                "source_sha256": source_digest, "copy_sha256": copy_digest}
    return {"file": source.name, "bytes": source_size, "sha256": source_digest,
            "action": "copied"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", metavar="CARD_MOUNT", help="Read-only mount of the card partition")
    parser.add_argument("destination", metavar="DIR", help="Where images are written")
    parser.add_argument("--manifest", metavar="FILE",
                        help="Manifest path (default DEST/manifest.json)")
    args = parser.parse_args(argv)

    source, destination = Path(args.source), Path(args.destination)
    if not source.is_dir():
        raise SystemExit(f"not a directory: {source}")
    images = sorted(source.glob("*.img"))
    if not images:
        raise SystemExit(f"no *.img files in {source}; did the dump run?")
    destination.mkdir(parents=True, exist_ok=True)

    results = [copy_verified(image, destination / image.name) for image in images]
    for extra in EXTRA_ARTIFACTS:
        candidate = source / extra
        if candidate.is_file():
            results.append(copy_verified(candidate, destination / candidate.name))

    for item in results:
        if item["action"] == "failed":
            print(f"  ✗ {item['file']:<20} 摘要不一致 source={item['source_sha256'][:16]}… "
                  f"copy={item['copy_sha256'][:16]}…")
        else:
            print(f"  {'=' if item['action'].startswith('skipped') else '✓'} "
                  f"{item['file']:<20} {item['bytes']:>12,} B  {item['sha256'][:16]}…")

    manifest_path = Path(args.manifest) if args.manifest else destination / "manifest.json"
    manifest = {"source": str(source), "images": [item for item in results
                                                  if item["file"].endswith(".img")]}
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    failed = [item for item in results if item["action"] == "failed"]
    total = sum(item["bytes"] for item in results if item["file"].endswith(".img"))
    print(f"\n  {len(images)} 个镜像，{total:,} B；失败 {len(failed)}；清单 {manifest_path}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
