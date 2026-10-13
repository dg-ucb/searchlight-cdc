# ruff: noqa: CPY001
"""Build deterministic Lambda/layer ZIPs and check them in the Lambda runtime."""

import argparse
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

RUNTIME_IMAGE = 'public.ecr.aws/lambda/python@sha256:88654a34882b5665ca04b0c8177422abb4d97a5ac2c68701e9fee04bb20c20d5'
COMPONENTS = ('function', 'boto3', 'psycopg2')
MAX_COMPRESSED = 50 * 1024 * 1024
MAX_UNCOMPRESSED = 250 * 1024 * 1024


def archive_tree(source: Path, destination: Path) -> None:
    """Normalize ordering, timestamps and permissions; reject unsafe source files."""
    if source.is_symlink() or not source.is_dir():
        raise ValueError(f'Expected an ordinary source directory: {source}')
    files = []
    for path in sorted(source.rglob('*')):
        name = path.relative_to(source).as_posix()
        if path.is_symlink() or '\\' in name or (not path.is_file() and not path.is_dir()):
            raise ValueError(f'Unsupported release path: {name}')
        if (
            not path.is_file()
            or any(p.startswith('.') or p == '__pycache__' for p in Path(name).parts)
            or path.suffix in ('.pyc', '.pyo')
            or path.name in ('RECORD', 'direct_url.json')
        ):
            continue
        files.append((name, path))
    if sum(path.stat().st_size for _, path in files) > MAX_UNCOMPRESSED:
        raise ValueError('Lambda uncompressed package limit exceeded')
    with zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, path in files:
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)


def check_sizes(release: Path) -> None:
    """Enforce Lambda's shared 250 MiB limit for the function and all layers."""
    total = 0
    for name in COMPONENTS:
        path = release / f'{name}.zip'
        if path.stat().st_size > MAX_COMPRESSED:
            raise ValueError(f'{name}.zip exceeds the 50 MiB compressed package limit')
        with zipfile.ZipFile(path) as archive:
            total += sum(entry.file_size for entry in archive.infolist())
    if total > MAX_UNCOMPRESSED:
        raise ValueError('Function and layers exceed the combined 250 MiB package limit')


def build(root: Path) -> None:
    """Package source plus hash-locked Linux wheels without SAM or AWS access."""
    release = root / 'dist/release'
    shutil.rmtree(release, ignore_errors=True)
    release.mkdir(parents=True)
    archive_tree(root / 'lambda', release / 'function.zip')
    with tempfile.TemporaryDirectory(prefix='searchlight-layers-') as directory:
        for name in COMPONENTS[1:]:
            layer = Path(directory) / name
            target = layer / 'python'
            subprocess.run(
                [
                    sys.executable,
                    '-m',
                    'pip',
                    'install',
                    '--disable-pip-version-check',
                    '--target',
                    str(target),
                    '--platform',
                    'manylinux2014_x86_64',
                    '--implementation',
                    'cp',
                    '--python-version',
                    '3.13',
                    '--abi',
                    'cp313',
                    '--only-binary=:all:',
                    '--require-hashes',
                    '--no-compile',
                    '-r',
                    str(root / 'requirements-layers' / f'{name}.txt'),
                ],
                check=True,
            )
            # Console scripts and pip provenance can contain machine-specific paths.
            shutil.rmtree(target / 'bin', ignore_errors=True)
            archive_tree(layer, release / f'{name}.zip')
    check_sizes(release)


def _extract(archive_path: Path, destination: Path) -> None:
    with zipfile.ZipFile(archive_path) as archive:
        for entry in archive.infolist():
            name = PurePosixPath(entry.filename)
            if name.is_absolute() or '..' in name.parts or '\\' in entry.filename or not name.parts or stat.S_ISLNK(entry.external_attr >> 16):
                raise ValueError(f'Unsafe archive path: {entry.filename}')
        archive.extractall(destination)


def smoke(root: Path, image: str = RUNTIME_IMAGE) -> None:
    """Import the exact ZIPs without network access or AWS credentials."""
    check_sizes(root / 'dist/release')
    with tempfile.TemporaryDirectory(prefix='searchlight-smoke-') as directory:
        task, opt = Path(directory) / 'task', Path(directory) / 'opt'
        task.mkdir()
        opt.mkdir()
        _extract(root / 'dist/release/function.zip', task)
        for name in COMPONENTS[1:]:
            _extract(root / 'dist/release' / f'{name}.zip', opt)
        subprocess.run(
            [
                shutil.which('docker') or 'docker',
                'run',
                '--rm',
                '--platform',
                'linux/amd64',
                '--network',
                'none',
                '--read-only',
                '--tmpfs',
                '/tmp',
                '--entrypoint',
                'python',
                '-e',
                'AWS_EC2_METADATA_DISABLED=true',
                '-e',
                'LOCAL_DEV=true',
                '-e',
                'PYTHONDONTWRITEBYTECODE=1',
                '-e',
                'PYTHONPATH=/var/task:/opt/python',
                '-v',
                f'{task}:/var/task:ro',
                '-v',
                f'{opt}:/opt:ro',
                '-w',
                '/var/task',
                image,
                '-c',
                (
                    'import boto3, botocore, psycopg2, handler; '
                    "assert all(m.__file__.startswith('/opt/python/') for m in (boto3, botocore, psycopg2)); "
                    "assert handler.__file__.startswith('/var/task/'); "
                    "assert callable(handler.lambda_handler); print('Lambda artifact imports passed')"
                ),
            ],
            check=True,
        )


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--smoke', action='store_true', help='Also import the built ZIPs in Docker')
    parser.add_argument('--runtime-image', default=RUNTIME_IMAGE)
    args = parser.parse_args()
    repository = Path(__file__).resolve().parents[1]
    build(repository)
    if args.smoke:
        smoke(repository, args.runtime_image)
