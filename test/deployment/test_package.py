# ruff: noqa: CPY001, PT009, PT027
"""Verify release archives are reproducible and safe to extract and deploy."""

import base64
import hashlib
import importlib.util
import io
import json
import os
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.request import Request

SPEC = importlib.util.spec_from_file_location('release_package', Path(__file__).resolve().parents[2] / 'scripts/package_lambda.py')
package = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(package)
SHARED = {name: f'arn:aws:lambda:us-east-1:123456789012:layer:shared-{name}:2' for name in ('boto3', 'psycopg2')}
DOWNLOAD_URL = 'https://awslambda-example-layers.s3.us-east-1.amazonaws.com/layer.zip?X-Amz-Signature=not-a-real-signature'


class Download(io.BytesIO):
    def geturl(self):
        return DOWNLOAD_URL


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

    def layer_payload(self, name):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive:
            archive.writestr(f'python/lib/python3.13/site-packages/{name}.py', f'PINNED = {name!r}\n')
        return stream.getvalue()

    def metadata(self, arn, payload):
        return {
            'LayerVersionArn': arn,
            'CompatibleRuntimes': ['python3.13'],
            'CompatibleArchitectures': ['x86_64'],
            'Content': {
                'Location': DOWNLOAD_URL,
                'CodeSize': len(payload),
                'CodeSha256': base64.b64encode(hashlib.sha256(payload).digest()).decode(),
            },
        }

    def test_shared_build_downloads_the_pinned_zip_bytes_without_pip(self):
        (self.root / 'lambda').mkdir()
        (self.root / 'lambda/handler.py').write_text('def lambda_handler(event, context): return None\n')
        payloads = {name: self.layer_payload(name) for name in ('boto3', 'psycopg2')}
        names = iter(('boto3', 'psycopg2'))
        downloads = iter(('boto3', 'psycopg2'))

        def aws(*args, **kwargs):
            name = next(names)
            self.assertEqual(args[0][1:], ['lambda', 'get-layer-version-by-arn', '--arn', SHARED[name], '--output', 'json'])
            return SimpleNamespace(stdout=json.dumps(self.metadata(SHARED[name], payloads[name])))

        with (
            patch.object(package.subprocess, 'run', side_effect=aws) as commands,
            patch.object(package._DOWNLOAD, 'open', side_effect=lambda *_a, **_k: Download(payloads[next(downloads)])),
        ):
            package.build(self.root, SHARED)
        self.assertEqual(commands.call_count, 2)
        for name in ('boto3', 'psycopg2'):
            self.assertEqual((self.root / f'dist/release/{name}.zip').read_bytes(), payloads[name])
        with zipfile.ZipFile(self.root / 'dist/release/function.zip') as archive:
            self.assertEqual(archive.namelist(), ['handler.py'])

    def test_shared_download_rejects_identity_checksum_compatibility_and_url_mismatch(self):
        payload = self.layer_payload('boto3')
        valid = self.metadata(SHARED['boto3'], payload)
        invalid_metadata = (
            valid | {'LayerVersionArn': SHARED['boto3'].removesuffix(':2') + ':3'},
            valid | {'CompatibleRuntimes': ['python3.12']},
            valid | {'CompatibleArchitectures': ['arm64']},
            valid | {'Content': valid['Content'] | {'CodeSha256': base64.b64encode(b'0' * 32).decode()}},
            valid | {'Content': valid['Content'] | {'Location': 'http://awslambda-example-layers.s3.amazonaws.com/layer.zip'}},
            valid | {'Content': valid['Content'] | {'Location': 'https://amazonaws.com.attacker.example/layer.zip'}},
            valid | {'Content': valid['Content'] | {'Location': 'https://user:password@s3.amazonaws.com/layer.zip'}},
            valid | {'Content': valid['Content'] | {'CodeSize': package.MAX_COMPRESSED + 1}},
            valid | {'Content': valid['Content'] | {'CodeSize': len(payload) + 1}},
        )
        for metadata in invalid_metadata:
            with (
                self.subTest(metadata=metadata),
                patch.object(package.subprocess, 'run', return_value=SimpleNamespace(stdout=json.dumps(metadata))),
                patch.object(package._DOWNLOAD, 'open', return_value=Download(payload)),
                self.assertRaises(ValueError),
            ):
                package.shared_layer(SHARED['boto3'], self.root / 'selected.zip')
        self.assertFalse((self.root / 'selected.zip').exists())

    def test_shared_download_enforces_stream_limit_even_without_reported_size(self):
        payload = self.layer_payload('boto3')
        metadata = self.metadata(SHARED['boto3'], payload)
        del metadata['Content']['CodeSize']
        with (
            patch.object(package.subprocess, 'run', return_value=SimpleNamespace(stdout=json.dumps(metadata))),
            patch.object(package._DOWNLOAD, 'open', return_value=Download(payload)),
            patch.object(package, 'MAX_COMPRESSED', len(payload) - 1),
            self.assertRaisesRegex(ValueError, 'compressed'),
        ):
            package.shared_layer(SHARED['boto3'], self.root / 'selected.zip')

    def test_download_redirect_rejects_non_aws_destinations_before_following(self):
        redirect = package._AWSRedirect()
        request = Request(DOWNLOAD_URL)
        for destination in ('http://s3.amazonaws.com/layer.zip', 'https://attacker.example/layer.zip', 'file:///tmp/layer.zip'):
            with self.subTest(destination=destination), self.assertRaisesRegex(ValueError, 'outside AWS'):
                redirect.redirect_request(request, None, 302, 'Found', {}, destination)

    def test_smoke_extracts_actual_shared_payloads_and_supports_both_python_layouts(self):
        release = self.root / 'dist/release'
        release.mkdir(parents=True)
        with zipfile.ZipFile(release / 'function.zip', 'w') as archive:
            archive.writestr('handler.py', 'SELECTED = True')
        for name in ('boto3', 'psycopg2'):
            (release / f'{name}.zip').write_bytes(self.layer_payload(name))

        def docker(args, **kwargs):
            self.assertIn('PYTHONPATH=/var/task:/opt/python/lib/python3.13/site-packages:/opt/python', args)
            self.assertEqual(args[args.index('--network') + 1], 'none')
            opt = Path(next(arg.removesuffix(':/opt:ro') for arg in args if arg.endswith(':/opt:ro')))
            for name in ('boto3', 'psycopg2'):
                self.assertEqual((opt / f'python/lib/python3.13/site-packages/{name}.py').read_text(), f'PINNED = {name!r}\n')

        with patch.object(package.subprocess, 'run', side_effect=docker):
            package.smoke(self.root)


if __name__ == '__main__':
    unittest.main()
