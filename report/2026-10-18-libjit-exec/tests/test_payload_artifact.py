"""Structural checks on the camera-side object.

The whole mechanism depends on four properties of the artifact, and a rebuild
with a different flag silently loses them: it must be AArch64, it must be a
shared object with no NEEDED libraries (the hosting process gives us nothing),
it must carry a DT_INIT entry (that is what dlopen runs, and everything else in
the loader fails), and it must stay small. Parsing the ELF directly keeps this
test free of binutils.
"""
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import libjit_requests as jr  # noqa: E402

ARTIFACT = Path(__file__).resolve().parents[1] / "scripts" / "payload" / "libjit_exec.so"

DT_NULL, DT_NEEDED, DT_INIT = 0, 1, 12
SHT_DYNAMIC = 6
EM_AARCH64 = 183
ET_DYN = 3


def dynamic_entries(blob):
    """Yield (tag, value) for every entry of the .dynamic section."""
    e_shoff, = struct.unpack_from("<Q", blob, 0x28)
    e_shentsize, e_shnum = struct.unpack_from("<HH", blob, 0x3A)
    for index in range(e_shnum):
        base = e_shoff + index * e_shentsize
        sh_type, = struct.unpack_from("<I", blob, base + 4)
        if sh_type != SHT_DYNAMIC:
            continue
        sh_offset, sh_size = struct.unpack_from("<QQ", blob, base + 24)
        for at in range(sh_offset, sh_offset + sh_size, 16):
            yield struct.unpack_from("<qQ", blob, at)


@pytest.fixture(scope="module")
def blob():
    if not ARTIFACT.is_file():
        pytest.skip(f"{ARTIFACT.name} not built; run scripts/payload/build.sh")
    return ARTIFACT.read_bytes()


def test_artifact_is_an_aarch64_shared_object(blob):
    assert blob.startswith(b"\x7fELF")
    assert blob[4] == 2, "expected ELF64"
    e_type, e_machine = struct.unpack_from("<HH", blob, 16)
    assert (e_type, e_machine) == (ET_DYN, EM_AARCH64)


def test_artifact_carries_a_dt_init_entry(blob):
    tags = dict(dynamic_entries(blob))
    assert tags.get(DT_INIT), "without DT_INIT dlopen runs nothing of ours"


def test_artifact_declares_no_needed_library(blob):
    tags = [tag for tag, _ in dynamic_entries(blob)]
    assert DT_NEEDED not in tags, "the hosting process may not provide any library"


def test_artifact_is_small_enough_for_the_loader_ramdisk(blob):
    assert len(blob) < jr.MAX_PAYLOAD
    assert len(blob) < 5 * 1024 * 1024, "the loader mounts a size=5M ramdisk"


def test_artifact_carries_its_own_record_tag(blob):
    assert b"A7JITE01" in blob
