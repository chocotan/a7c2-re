#!/usr/bin/env bash
# Offline, isolated build from the pinned source archive and this directory's patch.
set -euo pipefail
here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
archive="$(realpath -- "${1:-$here/source/vendor/qemu-11.1.2.tar.xz}")"
for tool in python3 bwrap patch ninja cc pkg-config flock make flex bison; do
  command -v "$tool" >/dev/null || { echo "Missing build dependency: $tool" >&2; exit 2; }
done
pkg-config --exists gtk+-3.0 sdl2 pixman-1 glib-2.0 libseccomp || {
  echo 'GTK3, SDL2, pixman, glib and libseccomp development files are required.' >&2; exit 2;
}
mkdir -p "$here/runtime/analysis" "$here/source/build"
exec 9>>"$here/runtime/analysis/.menu-probe.lock"
flock 9
stage="$(mktemp -d "$here/source/build/qemu-XXXXXX")"
python3 - "$here" "$archive" "$stage" <<'PY'
import hashlib,json,sys,tarfile,shutil
from pathlib import Path
root,archive,stage=map(Path,sys.argv[1:])
manifest=json.loads((root/'source/manifest.json').read_text())
for path,key in [(archive,'base_sha256'),(root/'source/qemu-sony-native.patch','patch_sha256')]:
    with path.open('rb') as f: actual=hashlib.file_digest(f,'sha256').hexdigest()
    if actual != manifest[key]: raise SystemExit('Source identity differs: '+str(path))
def qemu_filter(member,destination):
    # Unused EDK2 host-tool link; never bind an archive link to the host filesystem.
    if (member.name == 'qemu-11.1.2/roms/edk2/EmulatorPkg/Unix/Host/X11IncludeHack'
            and member.issym() and member.linkname == '/opt/X11/include'):
        print('SKIP_UNUSED_EDK2_ABSOLUTE_LINK',member.name)
        return None
    return tarfile.data_filter(member,destination)
with tarfile.open(archive) as source: source.extractall(stage,filter=qemu_filter)
dtc=root/'source/dtc'
expected=manifest['dtc_source_files']
actual={p.relative_to(dtc).as_posix() for p in dtc.rglob('*') if p.is_file()}
if actual != set(expected): raise SystemExit('DTC source inventory differs')
for name,sha in expected.items():
    path=dtc/name
    if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest()!=sha:
        raise SystemExit('DTC source identity differs: '+name)
shutil.copytree(dtc,stage/'qemu-11.1.2/subprojects/dtc',dirs_exist_ok=True)
PY
src="$stage/qemu-11.1.2"
patch --batch -p1 -d "$src" < "$here/source/qemu-sony-native.patch"
python3 - "$here/source/manifest.json" "$src" <<'PY'
import hashlib,json,sys
from pathlib import Path
manifest=json.loads(Path(sys.argv[1]).read_text()); root=Path(sys.argv[2])
for name,record in manifest['source_files'].items():
    if hashlib.sha256((root/name).read_bytes()).hexdigest()!=record['after']:
        raise SystemExit('Patched source differs: '+name)
print('PATCHED_SOURCE_IDENTITIES_PASS')
PY
mkdir -p "$stage/build" "$stage/tmp" "$stage/cache"
bwrap --unshare-net --die-with-parent --clearenv --ro-bind / / \
  --bind "$stage" "$stage" --dev /dev --proc /proc --chdir "$stage/build" \
  --setenv PATH /usr/bin:/bin --setenv LC_ALL C --setenv TMPDIR "$stage/tmp" \
  --setenv XDG_CACHE_HOME "$stage/cache" --setenv PYTHONDONTWRITEBYTECODE 1 \
  --setenv GIT_CEILING_DIRECTORIES "$stage" \
  --setenv PIP_NO_INDEX 1 --setenv PIP_NO_CACHE_DIR 1 \
  /bin/bash -c 'set -e
    "$1/configure" --target-list=aarch64-softmmu,aarch64-linux-user \
      --without-default-features --enable-tcg --enable-gtk --enable-sdl --enable-pixman \
      --enable-seccomp --enable-fdt=internal --disable-opengl --disable-download \
      --disable-docs --disable-containers --disable-werror --disable-tools
    ninja -j8 qemu-system-aarch64 qemu-aarch64
    cc -shared -fPIC -O2 -I "$1/subprojects/dtc/libfdt" \
      "$1"/subprojects/dtc/libfdt/*.c -Wl,-soname,libfdt.so.1 -o libfdt.so.1
    make -C "$1/subprojects/dtc" -j8 NO_PYTHON=1 NO_YAML=1 EXTRA_CFLAGS=-Wno-error dtc
    "$1/subprojects/dtc/dtc" -I dts -O dtb -o virt-packed.dtb "$2"
  ' build "$src" "$here/runtime/analysis/qemu-virt-board/virt-packed.dts"
QEMU_TEST_BINARY="$stage/build/qemu-system-aarch64" python3 -B "$here/runtime/analysis/test-qemu-native-input.py"
"$stage/build/qemu-system-aarch64" -display help | grep -x gtk
"$stage/build/qemu-aarch64" --version
python3 - "$stage/build/libfdt.so.1" <<'PY'
import ctypes,sys
library=ctypes.CDLL(sys.argv[1])
assert library.fdt_open_into and library.fdt_setprop
print('HOST_LIBFDT_SYMBOLS_PASS')
PY
base="$here/runtime/sources/qemu-11.1.2"
mkdir -p "$base/build-sony-ui" "$base/build-user" "$here/runtime/sources/qemu-tools/usr/lib" \
  "$here/runtime/sources/qemu-tools/usr/bin" "$here/runtime/analysis/qemu-virt-board"
install -m 0755 "$stage/build/qemu-system-aarch64" "$base/build-sony-ui/qemu-system-aarch64.new"
install -m 0755 "$stage/build/qemu-aarch64" "$base/build-user/qemu-aarch64.new"
install -m 0644 "$stage/build/libfdt.so.1" "$here/runtime/sources/qemu-tools/usr/lib/libfdt.so.1.new"
install -m 0755 "$src/subprojects/dtc/dtc" "$here/runtime/sources/qemu-tools/usr/bin/dtc.new"
install -m 0644 "$stage/build/virt-packed.dtb" "$here/runtime/analysis/qemu-virt-board/virt-packed.dtb.new"
mv -f "$base/build-sony-ui/qemu-system-aarch64.new" "$base/build-sony-ui/qemu-system-aarch64"
mv -f "$base/build-user/qemu-aarch64.new" "$base/build-user/qemu-aarch64"
mv -f "$here/runtime/sources/qemu-tools/usr/lib/libfdt.so.1.new" "$here/runtime/sources/qemu-tools/usr/lib/libfdt.so.1"
mv -f "$here/runtime/sources/qemu-tools/usr/bin/dtc.new" "$here/runtime/sources/qemu-tools/usr/bin/dtc"
mv -f "$here/runtime/analysis/qemu-virt-board/virt-packed.dtb.new" "$here/runtime/analysis/qemu-virt-board/virt-packed.dtb"
"$base/build-sony-ui/qemu-system-aarch64" -display help
(cd "$here" && sha256sum runtime/sources/qemu-11.1.2/build-sony-ui/qemu-system-aarch64 \
  runtime/sources/qemu-11.1.2/build-user/qemu-aarch64 \
  runtime/sources/qemu-tools/usr/lib/libfdt.so.1 runtime/sources/qemu-tools/usr/bin/dtc \
  runtime/analysis/qemu-virt-board/virt-packed.dtb) > "$here/source/build-programs.sha256"
printf 'Built and installed. Source/build logs remain in %s\n' "$stage"
