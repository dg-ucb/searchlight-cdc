# ruff: noqa: CPY001, PT009, PT027
"""Verify release archives are reproducible and safe to extract and deploy."""

import importlib.util
import os
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('release_package', Path(__file__).resolve().parents[2] / 'scripts/package_lambda.py')
package = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(package)


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / 'source'
        self.source.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def test_archive_is_stable_across_mtime_permissions_and_creation_order(self):
        for name in ('z.py', 'a.py'):
            (self.source / name).write_text(name)
        package.archive_tree(self.source, self.root / 'first.zip')
        for path in self.source.iterdir():
            os.utime(path, (1_000_000_000, 1_000_000_000))
            path.chmod(0o755)
        package.archive_tree(self.source, self.root / 'second.zip')
        self.assertEqual((self.root / 'first.zip').read_bytes(), (self.root / 'second.zip').read_bytes())
        with zipfile.ZipFile(self.root / 'first.zip') as archive:
            self.assertEqual(archive.namelist(), ['a.py', 'z.py'])
            self.assertEqual(archive.getinfo('a.py').date_time, (1980, 1, 1, 0, 0, 0))

    def test_generated_cache_and_pip_machine_provenance_are_excluded(self):
        for name in ('app.py', '.env', 'cached.pyc', 'RECORD', 'direct_url.json'):
            (self.source / name).write_text('data')
        (self.source / '__pycache__').mkdir()
        (self.source / '__pycache__/cached.py').write_text('cache')
        package.archive_tree(self.source, self.root / 'output.zip')
        with zipfile.ZipFile(self.root / 'output.zip') as archive:
            self.assertEqual(archive.namelist(), ['app.py'])

    def test_symlinks_and_backslash_source_names_are_rejected(self):
        outside = self.root / 'private'
        outside.write_text('secret')
        link = self.source / '.hidden-link'
        link.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, 'Unsupported release path'):
            package.archive_tree(self.source, self.root / 'output.zip')
        link.unlink()
        (self.source / '..\\escape').write_text('data')
        with self.assertRaises(ValueError):
            package.archive_tree(self.source, self.root / 'output.zip')

    def test_combined_uncompressed_and_individual_compressed_limits(self):
        (self.source / 'data').write_bytes(b'0' * 30)
        for name in package.COMPONENTS:
            package.archive_tree(self.source, self.root / f'{name}.zip')
        with patch.object(package, 'MAX_UNCOMPRESSED', 89), self.assertRaisesRegex(ValueError, 'combined'):
            package.check_sizes(self.root)
        with patch.object(package, 'MAX_COMPRESSED', 1), self.assertRaisesRegex(ValueError, 'compressed'):
            package.check_sizes(self.root)

    def test_oversized_source_is_rejected_before_archiving(self):
        (self.source / 'data').write_bytes(b'0' * 30)
        with patch.object(package, 'MAX_UNCOMPRESSED', 29), self.assertRaisesRegex(ValueError, 'uncompressed'):
            package.archive_tree(self.source, self.root / 'output.zip')

    def test_extraction_rejects_traversal_absolute_paths_and_symlinks(self):
        for name, mode in (('../escape', stat.S_IFREG), ('/escape', stat.S_IFREG), ('..\\escape', stat.S_IFREG), ('link', stat.S_IFLNK)):
            with self.subTest(name=name):
                with zipfile.ZipFile(self.root / 'unsafe.zip', 'w') as archive:
                    info = zipfile.ZipInfo(name)
                    info.external_attr = mode << 16
                    archive.writestr(info, 'outside')
                with self.assertRaisesRegex(ValueError, 'Unsafe archive path'):
                    package._extract(self.root / 'unsafe.zip', self.root / 'extract')
        self.assertFalse((self.root / 'escape').exists())


if __name__ == '__main__':
    unittest.main()
