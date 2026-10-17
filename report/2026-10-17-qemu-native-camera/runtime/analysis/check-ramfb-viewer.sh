#!/usr/bin/env bash
set -euo pipefail
here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
out="$here/ramfb-output/check"
mkdir -p "$out"
cc -O2 -Wall -Wextra -Werror "$here/test-ramfb-viewer.c" -o "$out/test-host"
"$out/test-host"
aarch64-linux-gnu-gcc -O2 -Wall -Wextra -Werror "$here/test-ramfb-viewer.c" -o "$out/test-aarch64"
"$here/../sources/qemu-11.1.2/build-user/qemu-aarch64" -L /usr/aarch64-linux-gnu "$out/test-aarch64"
clang --target=aarch64-linux-gnu --sysroot=/usr/aarch64-linux-gnu -fuse-ld=lld -nostdlib -O2 \
  -mno-outline-atomics -Wall -Wextra -Werror -Wl,--no-undefined -Wl,-e,_start \
  -Wl,--dynamic-linker=/lib/ld-linux-aarch64.so.1 -DRAMFB_FIRMWARE_TEST \
  "$here/test-ramfb-viewer.c" "$here/menu-root/lib/libc-2.25.so" "$here/menu-root/lib/ld-2.25.so" -o "$out/test-firmware"
"$here/../sources/qemu-11.1.2/build-user/qemu-aarch64" -L "$here/menu-root" "$out/test-firmware"
bash -n "$here/qemu-kernel-probe.sh" "$here/build-observers.sh"
python3 -m py_compile "$here/qemu-menu-probe.py"
python3 "$here/test-ramfb-layout.py"
