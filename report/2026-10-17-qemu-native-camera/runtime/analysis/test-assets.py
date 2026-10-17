#!/usr/bin/env python3
"""Small offline boundary checks; optional --runtime verifies real import."""
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import struct
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location('assets', Path(__file__).with_name('prepare-assets.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
RUNTIME = None
if '--runtime' in sys.argv:
    i = sys.argv.index('--runtime')
    RUNTIME = Path(sys.argv[i+1]).absolute()
    del sys.argv[i:i+2]


class AssetsTest(unittest.TestCase):
    def test_elf_boundaries(self):
        data = bytearray(128)
        data[:6] = b'\x7fELF\x02\x01'
        struct.pack_into('<H', data, 18, 183)
        struct.pack_into('<Q', data, 32, 64)
        struct.pack_into('<HH', data, 54, 56, 1)
        struct.pack_into('<IIQQQQQQ', data, 64, 1, 0, 120, 0x1000, 0, 8, 16, 8)
        elf = m.ELF(data)
        self.assertEqual(elf.offset(0x1000, 8), 120)
        self.assertEqual(elf.offset(0x1007, 1), 127)
        for va, length in [(0xfff, 1), (0x1007, 2), (0x1008, 1), (0x1000, 0)]:
            with self.assertRaises(ValueError):
                elf.offset(va, length)
        elf.loads.append(elf.loads[0])
        with self.assertRaises(ValueError):
            elf.offset(0x1000, 1)

    def test_fat_paths(self):
        with tempfile.TemporaryDirectory(prefix='assets space ') as tmp:
            for name, mode in [('/../escape', stat.S_IFREG), ('//absolute', stat.S_IFREG),
                               ('/a/../../escape', stat.S_IFREG), ('/C:/escape', stat.S_IFREG),
                               ('/a\\escape', stat.S_IFREG), ('/link', stat.S_IFLNK)]:
                entry = m.UnixFile(name, 1, 0, mode, 0, 0, io.BytesIO(b'x'))
                with patch.object(m, 'readFat', return_value=iter([entry])):
                    with self.assertRaises(ValueError):
                        m.extract_fat(b'', Path(tmp))
            with self.assertRaises(ValueError):
                m.relative('/absolute')
            entry = m.UnixFile('/valid file', 1, 0, stat.S_IFREG, 0, 0, io.BytesIO(b'x'))
            with patch.object(m, 'readFat', return_value=iter([entry])):
                m.extract_fat(b'', Path(tmp))
            self.assertEqual((Path(tmp)/'valid file').read_bytes(), b'x')

    def test_wrong_missing_version_and_existing_outputs(self):
        with tempfile.TemporaryDirectory(prefix='runtime space ') as tmp:
            root = Path(tmp)
            with self.assertRaises(FileNotFoundError):
                m.verified(root/'missing', '0'*64)
            (root/'wrong').write_bytes(b'wrong firmware')
            with self.assertRaises(ValueError):
                m.verified(root/'wrong', '0'*64)
            with self.assertRaises(ValueError):
                m.identity(b'wrong version')
            for name in m.OUTPUTS:
                path = root/name
                path.write_bytes(b'keep')
                with self.assertRaises(ValueError):
                    m.available(root)
                self.assertEqual(path.read_bytes(), b'keep')
                path.unlink()

    def test_guest_link_namespace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'bin').mkdir()
            (root/'bin/sh').symlink_to('/bin/busybox')
            (root/'bin/busybox').write_bytes(b'guest')
            self.assertEqual(m.guest_links(root)['bin/sh'], '/bin/busybox')
            with self.assertRaises(ValueError):
                m.local(root, 'bin/sh')
            (root/'escape').symlink_to('../outside')
            with self.assertRaises(ValueError):
                m.guest_links(root)
            (root/'escape').unlink()
            (root/'cycle').symlink_to('cycle')
            with self.assertRaises(ValueError):
                m.guest_links(root)

    @unittest.skipUnless(RUNTIME, 'pass --runtime for actual Sony asset checks')
    def test_installed_assets_and_exact_imdb(self):
        import subprocess
        manifest = m.check_runtime(RUNTIME)
        # Obtain original from the original image only when explicitly supplied.
        source = os.environ.get('SONY_TEST_USR_IMAGE')
        if not source:
            self.fail('SONY_TEST_USR_IMAGE required for original IMDB transformation test')
        original = subprocess.run(['debugfs', '-R', 'cat /lib/libIMDB.so', source], capture_output=True, check=True).stdout
        result = m.patch_imdb(original)
        self.assertEqual(m.sha(result), m.PATCH_SHA)
        self.assertEqual(sum(a != b for a, b in zip(original, result)), 10)
        self.assertEqual(len(manifest['patchedIMDB']['services']), 10)
        root = RUNTIME/'analysis/menu-root'
        for name in ['usr/lib/libgpe.so', 'seed-setting/Backup.bin']:
            path = root/name
            data, mode = path.read_bytes(), stat.S_IMODE(path.stat().st_mode)
            try:
                path.chmod(0o644)
                path.write_bytes(data+b'tamper')
                with self.assertRaises(ValueError):
                    m.check_runtime(RUNTIME)
            finally:
                path.write_bytes(data)
                path.chmod(mode)
        for name, target in [('escape-test', '../outside'), ('usr/lib/escape-test', '../../../../outside')]:
            path = root/name
            try:
                path.symlink_to(target)
                with self.assertRaises(ValueError):
                    m.check_runtime(RUNTIME)
            finally:
                path.unlink()
        extra = root/'seed-setting/private-test'
        try:
            extra.write_bytes(b'not allowed')
            with self.assertRaises(ValueError):
                m.check_runtime(RUNTIME)
        finally:
            extra.unlink()
        m.check_runtime(RUNTIME)

    @unittest.skipUnless(RUNTIME, 'pass --runtime for actual Sony asset checks')
    def test_manifest_cannot_cancel_or_rewrite_checks(self):
        path = RUNTIME/'analysis/native-assets.json'
        original_manifest = path.read_bytes()
        busybox = RUNTIME/'analysis/menu-root/bin/busybox'
        data, mode = busybox.read_bytes(), stat.S_IMODE(busybox.stat().st_mode)
        try:
            manifest = json.loads(original_manifest)
            busybox.chmod(0o644)
            busybox.write_bytes(data+b'changed')
            manifest['assets']['menu-root/bin/busybox']['sha256'] = m.digest(busybox)
            manifest['assets']['menu-root/bin/busybox']['size'] = busybox.stat().st_size
            path.write_text(json.dumps(manifest))
            with self.assertRaises(ValueError):
                m.check_runtime(RUNTIME)
            manifest['assets'] = {}
            path.write_text(json.dumps(manifest))
            busybox.unlink()
            with self.assertRaises(ValueError):
                m.check_runtime(RUNTIME)
        finally:
            busybox.write_bytes(data)
            busybox.chmod(mode)
            path.write_bytes(original_manifest)

    @unittest.skipUnless(RUNTIME, 'pass --runtime for actual Sony asset checks')
    def test_extra_private_settings_and_files(self):
        root = RUNTIME/'analysis/menu-root'
        for name in ('setting/Backup.bin', 'tmp/unlisted-private-file'):
            path = root/name
            self.assertFalse(path.exists())
            try:
                path.write_bytes(b'private fixture, not allowed')
                with self.assertRaises(ValueError):
                    m.check_runtime(RUNTIME)
            finally:
                path.unlink()

    @unittest.skipUnless(RUNTIME, 'pass --runtime for actual Sony asset checks')
    def test_executable_permission_is_required(self):
        path = RUNTIME/'analysis/menu-root/bin/busybox'
        mode = stat.S_IMODE(path.stat().st_mode)
        self.assertTrue(mode & 0o111)
        try:
            path.chmod(mode & ~0o111)
            with self.assertRaises(ValueError):
                m.check_runtime(RUNTIME)
        finally:
            path.chmod(mode)

    @unittest.skipUnless(RUNTIME, 'pass --runtime for actual Sony asset checks')
    def test_staging_uses_external_project_overlay(self):
        overlay = RUNTIME/'analysis/guest-overlay'
        moved = RUNTIME/'analysis/overlay-test-only'
        self.assertFalse(moved.exists())
        overlay.rename(moved)
        try:
            m.check_runtime(RUNTIME, moved)
            with self.assertRaises(FileNotFoundError):
                m.check_runtime(RUNTIME)
        finally:
            moved.rename(overlay)


if __name__ == '__main__':
    os.environ['TZ'] = 'CST-8'
    time.tzset()
    unittest.main()
