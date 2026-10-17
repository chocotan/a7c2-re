#!/usr/bin/env python3
"""Import only the pinned ILCE-7CM2 2.01 assets into a clean offline runtime."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import time

sys.dont_write_bytecode = True
KIT = Path(__file__).resolve().parent
sys.path.insert(0, str(KIT / 'vendor/fwtool'))
from fwtool.archive import UnixFile
from fwtool.archive.fat import readFat, writeFat

PART = '0700_part_image/dev/'
BACKUP = '0110_backup/SYSIPSX-DSLR/PP/CX89701_ALLLANG.bin'
SETTINGS = '0800_appli/setting/'
HEADER_SHA = 'ee176eb36cadf11d0f9d02f0829121352fa9d5804932a33831764618c23e8c5e'
IMDB_SHA = '14f3cd439bf27a6878b97d155c3cd97cc534a57395f922aed56e59a5b8b32f31'
PATCH_SHA = '0ebba8dd2fefad4684419d1111a56c659c7fd04473c04da39e6f2e7d8ac0facf'
FAT_SHA = '535d8f0570a4e56e04e71c400863ed39bbd735c85a9aa8e32748a136968d495c'
DAT4_SHA = 'a12871fee210fb8619291eaea194581cbd2531e4b23759d225f6806923f63222'
INPUTS = {
    PART+'nflasha3': (32752640, '5d8bff23ee1f2e464e36f086fb8e40fe22d3ac8c3b5b11d0a4a0f845a6f8f2c6'),
    PART+'nflasha8': (25102336, '8700f570aed3eb32038f313590759736a06162f402637518f8078dd51b6f1541'),
    PART+'nflasha15': (770030592, '621d152c075712369464248b0bfb46451220f6fc8e26a058a5ed5dcb898bd915'),
    BACKUP: (2390264, '3dab4f0d6b7790f4a7798034b57f43bfe3255c39dc6da2897fc288b3a218fe47'),
    SETTINGS+'DmmCfg.bin': (65956, '6c37baddaa60b6756340f77b051eed948978cea28d04f499f91263c9d270d962'),
    SETTINGS+'cxd90057.dtb': (16875, 'c5735ad9c6da8c48fb24eeee0bb743d07c486a47d5db19bef13b5eaea24ad280'),
    SETTINGS+'development.txt': (0, 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'),
    SETTINGS+'facedmy.bin': (51200, 'e1db981a5261c8eb234071bbfbd15116d3bfbb651e91dfc1175578adbf7ec1ba'),
    SETTINGS+'kemco.txt': (1731, '1cdf25cf232a86f8a80298245d944e5913ade69dbd11f0bf870dabed57ef8772'),
    SETTINGS+'ulogio.bin': (2900, 'cf2695f1cf9ef476cd228ea7e868d8c853896cae6d11d9876730c80d5a011404'),
}
KEYS = {
    'usr/lib/appFw.so': '2ea02f7ecbc5bb917316fe8ef4ce5ffd87eb66efaae78ba2ce11420552df539b',
    'usr/lib/libObj.so': 'c0897d72a40bf0a161fa7f89e9a34eef4d19e0506c7817a5c11a6f53897cdbbc',
    'usr/lib/libgpe.so': '80fc6236379933f80c2689aa002e0cf39483da8d45e876705d2a427cd1a2a7d1',
    'lib/libc-2.25.so': 'f9104526eb7e5d2ea533c1a209bb0b83e69367f569e4c966895c8e106e6382c9',
    'lib/libdl-2.25.so': '6dde01ab92843eaf8c330f4cbd9b47186b14843c4e1de9f7c68ecacb3414403b',
    'lib/libpthread-2.25.so': '7394f861930c45dd5076a700da5755faf0f6bed08544b0d60eadb85a14a7d592',
    'lib/ld-2.25.so': '90b4fe734ba7faa08d4591b1363bff478e7bde399a821fc0b2d021c062a58a9a',
    'cp/cpapp-b.bin': 'f5a477588698ef965748903b8959a39ce4f59ffd4aee4d4234ef1670f56a1428',
    'cp/DmmCfg.bin': 'd3466cee62fd86d927236a2c6c37dd633f76cdc181b4ad07a9863696aab76660',
}
SKIP = {'libUsbBusPower.so', 'libInfraUsbCmn.so', 'libsensapi.so',
        'libUsbControl.so', 'libBusPower.so', 'libHotplug.so', 'libInfraCrypt.so',
        'libInfraMediaPwrMgr.so', 'libNetServiceTethering.so', 'libTetheringResource.so'}
OUTPUTS = ('menu-root', 'unpacked', 'libIMDB-menu.so', 'firmware-identity.json', 'native-assets.json')
INVENTORY_SHA = '5b328a77983b7b5f6aaa23226bd239ca1991138af9b63e363c916f8fb9ad1ca4'
GENERATED = {'init', 'lib/observe-constructor.so'} | {
    'simulation/' + name for name in (
        'menu-observe.so', 'ramfb-viewer', 'headless-osd-export', 'menu-readmem',
        'menu-regs', 'input-table', 'menu-scanmem', 'check-menu-observe',
        'seed-power-on', 'seed-date-clock', 'seed-indicator-config', 'inject-key', 'inject-cp-event',
        'backup-dump', 'input-state.sh', 'mutex-trace.so', 'file-media-config.so',
        'fsys-file-test', 'media-template.fat')}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def relative(name):
    if not isinstance(name, str) or not name or name.startswith('/') or '\\' in name:
        raise ValueError(f'Unsafe relative path: {name!r}')
    if any(p in ('', '.', '..') or ':' in p or any(ord(c) < 32 for c in p) for p in name.split('/')):
        raise ValueError(f'Unsafe relative path: {name!r}')
    return name


def local(root, name):
    """Host access never follows any symlink, including the final component."""
    path = root
    for part in relative(name).split('/'):
        path = path / part
        if path.is_symlink():
            raise ValueError(f'Symlink in host access: {name}')
    return path


def ordinary(path):
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError(f'Expected regular file: {path.name}')
    return path


def verified(path, expected, size=None, mode=None):
    ordinary(path)
    if (size is not None and path.stat().st_size != size) or digest(path) != expected:
        raise ValueError(f'Wrong/missing Sony 2.01 asset: {path.name}')
    if mode is not None and stat.S_IMODE(path.stat().st_mode) != mode:
        raise ValueError(f'Changed asset permissions: {path.name}')


def trusted_inventory():
    raw = (KIT/'sony-assets.json').read_bytes()
    if sha(raw) != INVENTORY_SHA:
        raise ValueError('Trusted Sony inventory changed')
    return json.loads(raw)


class ELF:
    def __init__(self, data):
        self.data = data
        if len(data) < 64 or data[:6] != b'\x7fELF\x02\x01' or struct.unpack_from('<H', data, 18)[0] != 183:
            raise ValueError('Expected AArch64 little-endian ELF64')
        off = struct.unpack_from('<Q', data, 32)[0]
        entsize, count = struct.unpack_from('<HH', data, 54)
        if entsize != 56 or count > 1024 or off + entsize * count > len(data):
            raise ValueError('Invalid ELF program headers')
        self.loads = []
        for i in range(count):
            kind, _, fileoff, va, _, filesz, memsz, _ = struct.unpack_from('<IIQQQQQQ', data, off+i*entsize)
            if kind == 1:
                if filesz > memsz or fileoff + filesz > len(data):
                    raise ValueError('Invalid ELF PT_LOAD bounds')
                self.loads.append((va, filesz, fileoff))

    def offset(self, va, size):
        hits = [off+va-start for start, length, off in self.loads
                if size > 0 and start <= va and va + size <= start + length]
        if len(hits) != 1:
            raise ValueError(f'ELF VA unmapped/ambiguous: {va:#x}+{size}')
        return hits[0]

    def number(self, va, fmt):
        return struct.unpack_from(fmt, self.data, self.offset(va, struct.calcsize(fmt)))[0]

    def cstr(self, va):
        chars = bytearray()
        for i in range(256):
            value = self.data[self.offset(va+i, 1)]
            if not value:
                return chars.decode('ascii')
            chars.append(value)
        raise ValueError('Unterminated ELF service name')


def patch_imdb(data):
    if sha(data) != IMDB_SHA:
        raise ValueError('Wrong original IMDB')
    elf, out, changed = ELF(data), bytearray(data), []
    for va in range(0x1db20, 0x21d80, 0x48):
        if elf.number(va, '<I') == 40:
            break
        ptr = elf.number(va+16, '<Q')
        name = elf.cstr(ptr) if ptr else ''
        if name in SKIP:
            mask = elf.number(va+8, '<I')
            if not mask & 1 or name in changed:
                raise ValueError('Unexpected IMDB entry')
            struct.pack_into('<I', out, elf.offset(va+8, 4), mask & ~7)
            changed.append(name)
    if len(changed) != 10 or set(changed) != SKIP or sha(out) != PATCH_SHA:
        raise ValueError('IMDB exact transformation mismatch')
    return bytes(out)


def extract_fat(data, destination):
    seen, total = set(), 0
    for entry in readFat(io.BytesIO(data)):
        # fwtool emits a single synthetic leading slash; never strip arbitrary slashes.
        if not entry.path.startswith('/'):
            raise ValueError('Invalid fwtool FAT path')
        name = relative(entry.path[1:])
        if name in seen or len(seen) >= 64 or entry.size > 32*1024*1024:
            raise ValueError('Duplicate or oversized FAT entry')
        seen.add(name)
        target = local(destination, name)
        if stat.S_ISDIR(entry.mode):
            target.mkdir(parents=True, exist_ok=True)
        elif stat.S_ISREG(entry.mode):
            total += entry.size
            if total > 64*1024*1024:
                raise ValueError('FAT extraction exceeds bound')
            target.parent.mkdir(parents=True, exist_ok=True)
            contents = entry.contents.read(entry.size+1)
            if len(contents) != entry.size:
                raise ValueError('Truncated FAT file')
            with target.open('xb') as output:
                output.write(contents)
            target.chmod(0o644)
            os.utime(target, (entry.mtime, entry.mtime))
        else:
            raise ValueError('FAT symlinks/special files forbidden')


def guest_links(root):
    """Resolve absolute guest links against guest /, never the host /."""
    links = {}
    for parent, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(parent)/name
            if path.is_symlink():
                links[path.relative_to(root).as_posix()] = os.readlink(path)
    for name, target in links.items():
        pending = target.split('/')
        stack = [] if target.startswith('/') else name.split('/')[:-1]
        hops = 0
        while pending:
            part = pending.pop(0)
            if part in ('', '.'):
                continue
            if part == '..':
                if not stack:
                    raise ValueError(f'Guest symlink escapes root: {name}')
                stack.pop()
                continue
            stack.append(part)
            nested = links.get('/'.join(stack))
            if nested is not None:
                hops += 1
                if hops > 40:
                    raise ValueError(f'Guest symlink cycle: {name}')
                stack = [] if nested.startswith('/') else stack[:-1]
                pending = nested.split('/') + pending
    return links


def identity(header):
    if len(header) != 512 or sha(header) != HEADER_SHA or header[:8] != b'UDTRFIRM':
        raise ValueError('Wrong Sony 2.01 FDAT identity header')
    model, region = struct.unpack_from('<II', header, 36)
    if (model, region, header[32:34]) != (0x20030014, 0, b'\x01\x02'):
        raise ValueError('Wrong model/version')
    contents = {'/DAT2': header[36:40], '/DAT3': header[40:44]}
    files = [UnixFile(n, len(d), 315532800, stat.S_IFREG|0o444, 0, 0, io.BytesIO(d)) for n, d in contents.items()]
    image = io.BytesIO()
    writeFat(files, 4*1024*1024, image)
    image.seek(0)
    if {f.path: f.contents.read() for f in readFat(image)} != contents or sha(image.getvalue()) != FAT_SHA:
        raise ValueError('Identity FAT roundtrip/hash mismatch')
    return image.getvalue(), header[32:34]


def record(path, source_class):
    if path.is_symlink():
        return {'source_class': source_class, 'symlink': os.readlink(path)}
    ordinary(path)
    return {'source_class': source_class, 'size': path.stat().st_size,
            'sha256': digest(path), 'mode': stat.S_IMODE(path.stat().st_mode)}


def inventory(analysis):
    result = {}
    for parent, dirs, files in os.walk(analysis, followlinks=False):
        for name in sorted(files + [n for n in dirs if (Path(parent)/n).is_symlink()]):
            path = Path(parent)/name
            rel = path.relative_to(analysis).as_posix()
            cls = 'sony_original'
            if rel == 'libIMDB-menu.so' or rel.endswith('/libIMDB.so') or 'firmware-identity' in rel or rel.endswith('firmware-version.dat4'):
                cls = 'sony_derived'
            if rel.endswith('/diag.sh'):
                cls = 'project_overlay'
            result[rel] = record(path, cls)
    return result


def anchors(analysis):
    root = local(analysis, 'menu-root')
    for name, expected in KEYS.items():
        verified(local(root, name), expected)
    for name in ['libIMDB-menu.so', 'menu-root/usr/lib/libIMDB.so', 'menu-root/simulation/libIMDB.so']:
        verified(local(analysis, name), PATCH_SHA)
    for name in ['seed-backup.bin', 'seed-setting/Backup.bin']:
        verified(local(root, name), INPUTS[BACKUP][1])
    for name, (_, expected) in INPUTS.items():
        if name.startswith(SETTINGS):
            verified(local(root, 'seed-setting/'+name.removeprefix(SETTINGS)), expected)
            verified(local(analysis, 'unpacked/firmware.tar_unpacked/'+name), expected)
    expected_names = {n.removeprefix(SETTINGS) for n in INPUTS if n.startswith(SETTINGS)} | {'Backup.bin'}
    if {p.name for p in local(root, 'seed-setting').iterdir()} != expected_names:
        raise ValueError('Unexpected seed-setting content')
    verified(local(analysis, 'unpacked/firmware.tar_unpacked/'+BACKUP), INPUTS[BACKUP][1])
    for name, expected in [('vmlinux.bin','620a6a3c2b4f09a3696f2fbf06bab98b49be5eaad6bdb366a1f33319fcb6e9d2'),
                           ('initrd.img','b249308ff1a42f8f441cedbd6244e8ffa0b4c88b984b2d5e71a436d09ab86529')]:
        verified(local(analysis, 'unpacked/firmware.tar_unpacked/'+PART+'nflasha3_unpacked/'+name), expected)
    verified(local(root, 'simulation/firmware-identity.fat'), FAT_SHA)
    verified(local(root, 'simulation/firmware-version.dat4'), DAT4_SHA)
    if os.path.lexists(root/'bin/rootshd'):
        raise ValueError('Unexpected rootshd contamination')
    if any(local(root, 'setting').iterdir()):
        raise ValueError('Installed setting must be empty; guest uses the validated seed')
    guest_links(root)
    for parent, dirs, files in os.walk(local(analysis, 'unpacked'), followlinks=False):
        if any((Path(parent)/n).is_symlink() for n in dirs+files):
            raise ValueError('Symlink in unpacked assets')


def check_runtime(runtime, overlay=None):
    analysis = local(runtime, 'analysis')
    overlay = overlay or analysis/'guest-overlay'
    manifest = json.loads(ordinary(local(analysis, 'native-assets.json')).read_text())
    if manifest.get('schema') != 2 or manifest.get('model') != 'ILCE-7CM2' or manifest.get('version') != '2.01' or manifest.get('seed_policy') != 'alllang':
        raise ValueError('Invalid native assets manifest/version')
    expected_inputs = {n: {'source_class': 'sony_original', 'size': s, 'sha256': h} for n, (s, h) in INPUTS.items()}
    if manifest.get('inputs') != expected_inputs or manifest.get('fdat_header') != {'source_class': 'sony_original', 'size': 512, 'sha256': HEADER_SHA}:
        raise ValueError('Changed input provenance')
    patch = manifest['patchedIMDB']
    if (patch.get('source_class'), patch.get('input_sha256'), patch.get('output_sha256'),
        patch.get('include_default'), patch.get('clear_mask'), patch.get('services')) != (
            'sony_derived', IMDB_SHA, PATCH_SHA, True, 7, sorted(SKIP)):
        raise ValueError('Changed IMDB provenance')
    anchors(analysis)
    trusted = trusted_inventory()
    overlay_name = 'menu-root/simulation/diag.sh'
    if set(manifest['assets']) != set(trusted) | {overlay_name}:
        raise ValueError('Asset inventory entries changed')
    for name, expected in trusted.items():
        if manifest['assets'][name] != expected:
            raise ValueError(f'Untrusted asset metadata: {name}')
    expected_overlay = record(local(overlay, 'simulation/diag.sh'), 'project_overlay')
    expected_overlay['mode'] = 0o755
    if manifest['assets'][overlay_name] != expected_overlay:
        raise ValueError('Changed project diag overlay')
    for name, expected in manifest['assets'].items():
        relative(name)
        path = analysis/name
        if 'symlink' in expected:
            local(analysis, str(Path(name).parent))
            if not path.is_symlink() or os.readlink(path) != expected['symlink']:
                raise ValueError(f'Changed symlink: {name}')
        else:
            verified(local(analysis, name), expected['sha256'], expected['size'], expected['mode'])
    for parent, dirs, files in os.walk(analysis/'menu-root', followlinks=False):
        for name in files + [n for n in dirs if (Path(parent)/n).is_symlink()]:
            path = Path(parent)/name
            rel = path.relative_to(analysis/'menu-root').as_posix()
            if 'menu-root/'+rel in trusted or 'menu-root/'+rel == overlay_name:
                continue
            if rel in GENERATED:
                ordinary(path)
            elif local(overlay, rel).is_file():
                verified(path, digest(local(overlay, rel)))
            else:
                raise ValueError(f'Unlisted guest file: {rel}')
    return manifest


def available(analysis):
    for name in OUTPUTS:
        if os.path.lexists(analysis/name):
            raise ValueError(f'Refusing existing output: {name}')


def import_assets(firmware, fdat, runtime):
    analysis = local(runtime, 'analysis')
    available(analysis)
    diag = local(analysis, 'guest-overlay/simulation/diag.sh')
    if not diag.exists():
        raise ValueError('Missing dependency: runtime/analysis/guest-overlay/simulation/diag.sh; install the main guest overlay first')
    ordinary(diag)
    if diag.stat().st_size > 1024*1024:
        raise ValueError('Oversized diag overlay')
    for name, (size, expected) in INPUTS.items():
        verified(local(firmware, name), expected, size)
    with ordinary(fdat).open('rb') as stream:
        header = stream.read(512)
    fat, dat4 = identity(header)
    analysis.mkdir(parents=True, exist_ok=True)
    local(runtime, 'sources').mkdir(exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.native-assets-staging-', dir=analysis))
    print(f'Staging: {stage}', flush=True)
    try:
        a = stage/'analysis'
        a.mkdir()
        unpacked = a/'unpacked/firmware.tar_unpacked'
        boot = unpacked/(PART+'nflasha3_unpacked')
        cp = unpacked/(PART+'nflasha8_unpacked')
        for name, dest in [('nflasha3', boot), ('nflasha8', cp)]:
            data = local(firmware, PART+name).read_bytes()
            if sha(data) != INPUTS[PART+name][1]:
                raise ValueError('Input changed during import')
            extract_fat(data, dest)
        verified(boot/'initrd.img', 'b249308ff1a42f8f441cedbd6244e8ffa0b4c88b984b2d5e71a436d09ab86529')
        root = a/'menu-root'
        root.mkdir()
        for image, target in [(boot/'initrd.img', 'menu-root'), (local(firmware, PART+'nflasha15'), 'menu-root/usr')]:
            local(a, target).mkdir(exist_ok=True)
            # Only controlled relative names enter debugfs's command parser.
            run = subprocess.run(['debugfs', '-R', f'rdump / {target}', str(image)], cwd=a,
                                 env={**os.environ, 'LC_ALL': 'C'}, capture_output=True, text=True, timeout=180)
            (stage/(target.replace('/', '-')+'-debugfs.log')).write_text(run.stdout+run.stderr)
            # Unprivileged extraction cannot chown to guest UID 0; bytes/modes still extract.
            errors = [line for line in (run.stdout+run.stderr).splitlines()
                      if line and not line.startswith('debugfs ')
                      and not line.startswith(('dump_file: Operation not permitted while changing ownership of ',
                                               'rdump: Operation not permitted while changing ownership of '))]
            if run.returncode or errors:
                raise ValueError('debugfs extraction failed; see staging logs')
            guest_links(root)
        verified(local(firmware, PART+'nflasha15'), INPUTS[PART+'nflasha15'][1], INPUTS[PART+'nflasha15'][0])
        for name in ['system', 'cp', 'setting', 'log', 'cert', 'lens', 'tmp', 'proc', 'sys', 'dev', 'tmp_wbshd', 'simulation', 'seed-setting']:
            local(root, name).mkdir(exist_ok=True)
        shutil.copytree(cp, local(root, 'cp'), dirs_exist_ok=True)
        for path in sorted(boot.iterdir()):
            if path.suffix in ('.bin', '.hcd'):
                shutil.copy2(path, local(root, 'system/'+path.name))
        for name, (size, expected) in INPUTS.items():
            if name.startswith(SETTINGS) or name == BACKUP:
                dest = local(unpacked, name)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(local(firmware, name), dest)
                verified(dest, expected, size)
                if name.startswith(SETTINGS):
                    shutil.copyfile(dest, local(root, 'seed-setting/'+Path(name).name))
        for name in ['seed-backup.bin', 'seed-setting/Backup.bin']:
            shutil.copyfile(unpacked/BACKUP, local(root, name))
        original = local(root, 'usr/lib/libIMDB.so')
        patched = patch_imdb(original.read_bytes())
        for path in [a/'libIMDB-menu.so', original, local(root, 'simulation/libIMDB.so')]:
            if path.exists():
                path.chmod(0o755)
            path.write_bytes(patched)
            path.chmod(0o555)
        shutil.copyfile(diag, local(root, 'simulation/diag.sh'))
        (root/'simulation/diag.sh').chmod(0o755)
        (root/'simulation/firmware-identity.fat').write_bytes(fat)
        (root/'simulation/firmware-version.dat4').write_bytes(dat4)
        provenance = {'header_sha256': HEADER_SHA, 'model': '0x20030014', 'region': 0,
                      'version_bcd_bytes': '0102', 'fat_sha256': FAT_SHA,
                      'source_class': 'sony_derived', 'scope': 'virtual identity; not original nflasha1'}
        (a/'firmware-identity.json').write_text(json.dumps(provenance, indent=2)+'\n')
        anchors(a)
        # Pin extraction permissions independently of the caller's umask.
        for name, expected in trusted_inventory().items():
            if 'symlink' not in expected:
                path = local(a, name)
                verified(path, expected['sha256'], expected['size'])
                path.chmod(expected['mode'])
        manifest = {'schema': 2, 'model': 'ILCE-7CM2', 'version': '2.01', 'seed_policy': 'alllang',
                    'inputs': {n: {'source_class': 'sony_original', 'size': s, 'sha256': h} for n, (s, h) in INPUTS.items()},
                    'fdat_header': {'source_class': 'sony_original', 'size': 512, 'sha256': HEADER_SHA},
                    'patchedIMDB': {'source_class': 'sony_derived', 'input_sha256': IMDB_SHA, 'output_sha256': PATCH_SHA,
                                    'include_default': True, 'clear_mask': 7, 'va_start': '0x1db20', 'va_end': '0x21d80', 'stride': 72, 'services': sorted(SKIP)},
                    'identity': provenance, 'assets': inventory(a)}
        (a/'native-assets.json').write_text(json.dumps(manifest, sort_keys=True, indent=2)+'\n')
        check_runtime(stage, analysis/'guest-overlay')
        available(analysis)
        for name in OUTPUTS:
            # Manifest is installed last: partial installs never have a success marker.
            (a/name).rename(analysis/name)
        print(f'Installed {len(manifest["assets"])} assets; seed_policy=alllang', flush=True)
    except Exception:
        print(f'IMPORT FAILED; preserved staging: {stage}', file=sys.stderr)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--firmware-root', type=Path)
    parser.add_argument('--fdat', type=Path)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--check-runtime', action='store_true')
    args = parser.parse_args()
    # The historical identity FAT was made at UTC+8; pin it without a timezone DB.
    os.environ['TZ'] = 'CST-8'
    time.tzset()
    try:
        runtime = args.runtime.absolute()
        local(Path(runtime.anchor), str(runtime).lstrip('/'))
        if args.check_runtime:
            check_runtime(runtime)
            print('CHECK_RUNTIME_PASS ILCE-7CM2 2.01 all lang')
        else:
            if args.firmware_root is None or args.fdat is None:
                parser.error('--firmware-root and --fdat are required for import')
            firmware, fdat = args.firmware_root.absolute(), args.fdat.absolute()
            for path in (firmware, fdat):
                local(Path(path.anchor), str(path).lstrip('/'))
            import_assets(firmware, fdat, runtime)
    except (OSError, ValueError, KeyError, struct.error, subprocess.SubprocessError) as exc:
        parser.exit(1, f'Asset import/check failed: {exc}\n')


if __name__ == '__main__':
    main()
