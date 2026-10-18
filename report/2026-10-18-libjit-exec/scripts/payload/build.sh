#!/bin/sh
# Build the camera-side runner. Reproducibility matters here: the previous
# deliverable shipped binaries without a recorded toolchain, so record both the
# command and the compiler version next to the artifact.
#
#   sh build.sh            # writes libjit_exec.so + BUILD.txt
#
# Requirements: an aarch64 cross compiler (aarch64-linux-gnu-gcc or a
# `-target aarch64-linux-gnu` capable clang). No libc is linked.
set -eu
cd "$(dirname "$0")"
OUT=libjit_exec.so

if command -v aarch64-linux-gnu-gcc >/dev/null 2>&1; then
    CC=aarch64-linux-gnu-gcc
    VERSION=$($CC --version | head -1)
else
    echo "aarch64-linux-gnu-gcc not found; install gcc-aarch64-linux-gnu" >&2
    exit 1
fi

# -nostdlib/-nostartfiles: no libc, no NEEDED entries -- the object must run
#   inside a process whose own mappings we do not control.
# -z max-page-size=4096: default 64 KiB page alignment pads the file with a
#   64 KiB gap between load segments; the camera's page size is 4 KiB.
$CC -shared -nostdlib -nostartfiles -fPIC -O2 -fno-builtin -s \
    -Wl,-z,max-page-size=4096 -o "$OUT" libjit_exec.c

{
    printf 'artifact: %s\n' "$OUT"
    printf 'compiler: %s\n' "$VERSION"
    printf 'command:  %s -shared -nostdlib -nostartfiles -fPIC -O2 -fno-builtin -s -Wl,-z,max-page-size=4096 -o %s libjit_exec.c\n' "$CC" "$OUT"
    printf 'sha256:   %s\n' "$(sha256sum "$OUT" | cut -d' ' -f1)"
    printf 'bytes:    %s\n' "$(wc -c < "$OUT")"
} > BUILD.txt

cat BUILD.txt
