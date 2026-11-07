"""Required mount checks and fail-closed, fresh native snapshots."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time


def validate_required_sources(config):
    sources = config.get('required-sources', [])
    if not isinstance(sources, list):
        raise ValueError('required-sources must be a list')
    for source in sources:
        root = Path(source['path'])
        if not root.is_absolute() or not root.is_dir():
            raise ValueError('Required source directory is missing: %s' % root)
        if 'marker' in source:
            marker = Path(source['marker'])
            resolved = (root / marker).resolve()
            if marker.is_absolute() or not resolved.is_relative_to(root.resolve()) or not resolved.is_file():
                raise ValueError('Required source marker is missing or outside source: %s' % marker)


def _timeout(config, default):
    value = float(config.get('timeout', default))
    if not 0 < value < float('inf'):
        raise ValueError('timeout must be positive and finite')
    return value


def _manifest(directory, kind, files):
    entries = []
    for name in files:
        path = directory / name
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError('Snapshot output is missing or empty')
        path.chmod(0o600)
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(block)
        entries.append({'file': name, 'size': path.stat().st_size, 'sha256': digest.hexdigest()})
    (directory / 'manifest.json').write_text(json.dumps({'producer': kind, 'files': entries}, indent=2) + '\n')


def _sqlite(directory, config):
    databases = config['databases']
    if not isinstance(databases, list) or not databases:
        raise ValueError('sqlite.databases must be a nonempty list')
    names = set()
    excludes = []
    timeout = _timeout(config, 60)
    for database in databases:
        name = database['name']
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', name) or name in names:
            raise ValueError('SQLite names must be safe and unique')
        names.add(name)
        path = Path(database['path'])
        if not path.is_absolute() or not path.is_file():
            raise ValueError('SQLite source must be an existing absolute file')
        deadline = time.monotonic() + timeout
        def progress(status, remaining, total):
            if time.monotonic() >= deadline:
                raise TimeoutError('SQLite online backup timed out')
        source = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=timeout)
        try:
            destination = sqlite3.connect(str(directory / (name + '.db')), timeout=timeout)
            try:
                source.backup(destination, pages=256, progress=progress, sleep=0.05)
                if destination.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                    raise ValueError('SQLite snapshot quick_check failed')
                destination.execute('PRAGMA journal_mode=DELETE').fetchone()
            finally:
                destination.close()
        finally:
            source.close()
        if database.get('exclude-source', True):
            excludes.extend(str(path) + suffix for suffix in ('', '-wal', '-shm', '-journal'))
    _manifest(directory, 'sqlite', [name + '.db' for name in sorted(names)])
    return excludes


def _talos(directory, config):
    credential = Path(config['talosconfig'])
    if not credential.is_absolute() or not credential.is_file():
        raise ValueError('talosconfig must be an existing absolute file')
    args = ['talosctl', '--talosconfig', str(credential)]
    for key in ('endpoints', 'nodes'):
        values = config[key]
        if not isinstance(values, list) or not values or not all(isinstance(v, str) and v and not v.startswith('-') and ',' not in v for v in values):
            raise ValueError('Talos %s must be a nonempty list of addresses' % key)
        if key == 'nodes' and len(values) != 1:
            raise ValueError('Talos snapshots require exactly one selected node')
        args.extend(['--' + key, ','.join(values)])
    args.extend(['etcd', 'snapshot', str(directory / 'snapshot.db')])
    # Do not log CLI output: it can contain sensitive connection information.
    subprocess.run(args, check=True, timeout=_timeout(config, 300), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    snapshot = directory / 'snapshot.db'
    size = snapshot.stat().st_size
    if size <= 32 or (size - 32) % 512:
        raise ValueError('Talos snapshot is truncated or missing checksum')
    digest = hashlib.sha256()
    with snapshot.open('rb') as stream:
        remaining = size - 32
        while remaining:
            block = stream.read(min(1024 * 1024, remaining))
            if not block:
                raise ValueError('Talos snapshot is truncated')
            digest.update(block)
            remaining -= len(block)
        if not hmac.compare_digest(digest.digest(), stream.read(32)):
            raise ValueError('Talos snapshot checksum mismatch')
    _manifest(directory, 'talos-etcd', ['snapshot.db'])
    return []


def produce(root, config):
    root = Path(root)
    exclusions = []
    staging_root = root / '.backup-client-staging'
    if any(kind in config for kind in ('sqlite', 'talos-etcd')):
        if staging_root.is_symlink() or staging_root.is_mount():
            raise ValueError('Reserved staging directory must not be a symlink or mount')
        staging_root.mkdir(mode=0o700, exist_ok=True)
        exclusions.append(str(staging_root))
    if 'talos-etcd' in config:
        credential = Path(config['talos-etcd']['talosconfig']).resolve()
        if credential.is_relative_to(root.resolve()):
            raise ValueError('Mount talosconfig outside BACKUP_ROOT; credentials must not be backed up')
    for kind, producer in (('sqlite', _sqlite), ('talos-etcd', _talos)):
        if kind not in config:
            continue
        final = root / kind
        # Never allow producer output replacement to remove a source or credentials.
        protected = [Path(s['path']) for s in config.get('required-sources', [])]
        protected += [Path(s['path']) for s in config.get('sqlite', {}).get('databases', [])]
        if 'talos-etcd' in config:
            protected.append(Path(config['talos-etcd']['talosconfig']))
        if any(p.resolve().is_relative_to(final.resolve()) or final.resolve().is_relative_to(p.resolve()) for p in protected):
            raise ValueError('Snapshot output directory overlaps a protected source')
        if final.is_symlink():
            raise ValueError('Snapshot output must not be a symlink')
        if final.exists():
            ownership = final / '.backup-client-managed'
            if final.is_mount() or not final.is_dir() or ownership.is_symlink() or not ownership.is_file() or ownership.read_text() != kind + '\n':
                raise ValueError('Refusing to replace unowned producer output directory')
            # Resolved final is the known, owned producer output; never remove mounts.
            if any(Path(base, name).is_mount() for base, dirs, files in os.walk(final) for name in dirs):
                raise ValueError('Producer output contains a mount')
            shutil.rmtree(final)
        staging = Path(tempfile.mkdtemp(prefix=kind + '-', dir=staging_root))
        try:
            exclusions.extend(producer(staging, config[kind]))
            (staging / '.backup-client-managed').write_text(kind + '\n')
            os.replace(staging, final)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    return exclusions
