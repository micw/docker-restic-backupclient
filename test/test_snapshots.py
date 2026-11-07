import os
import hashlib
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import backup_client as client
import snapshot_producers as snapshots


class SnapshotTests(unittest.TestCase):
    def test_required_missing_never_touches_repository(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for source in ({'path': str(root / 'missing')}, {'path': tmp, 'marker': '.backup-source'}, {'path': tmp, 'marker': '../escape'}):
                with self.subTest(source=source), patch.dict(os.environ, {'BACKUP_ROOT': tmp}), patch.object(client, 'load_config', return_value={'required-sources': [source]}), patch.object(client, 'init_restic_repo') as init, patch.object(client.subprocess, 'check_call') as command:
                    self.assertFalse(client.run_backup(True))
                    init.assert_not_called()
                    command.assert_not_called()
            self.assertFalse((root / 'missing').exists())
            self.assertFalse((root / '.backup-source').exists())
            (root / '.backup-source').write_text('mounted')
            snapshots.validate_required_sources({'required-sources': [{'path': tmp, 'marker': '.backup-source'}]})

    def test_sqlite_wal_snapshot_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'live.db'
            db = sqlite3.connect(source)
            try:
                db.execute('PRAGMA journal_mode=WAL')
                db.execute('CREATE TABLE records(value TEXT)')
                db.execute("INSERT INTO records VALUES ('committed WAL row')")
                db.commit()
                config = {'sqlite': {'databases': [{'name': 'ha', 'path': str(source)}]}}
                excludes = snapshots.produce(root, config)
                self.assertEqual(excludes, [str(root / '.backup-client-staging')] + [str(source) + suffix for suffix in ('', '-wal', '-shm', '-journal')])
                self.assertEqual((root / 'sqlite' / 'ha.db').stat().st_mode & 0o777, 0o600)
                restored = sqlite3.connect(root / 'sqlite' / 'ha.db')
                try:
                    self.assertEqual(restored.execute('SELECT * FROM records').fetchall(), [('committed WAL row',)])
                    self.assertEqual(restored.execute('PRAGMA journal_mode').fetchone(), ('delete',))
                finally:
                    restored.close()
                self.assertEqual(db.execute('PRAGMA journal_mode').fetchone(), ('wal',))
            finally:
                db.close()

    def test_talos_cli_atomic_manifest_and_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            credential = root / 'talosconfig'
            root = root / 'backup'
            root.mkdir()
            credential.write_text('synthetic-secret')
            config = {'talos-etcd': {'talosconfig': str(credential), 'endpoints': ['endpoint'], 'nodes': ['node'], 'timeout': 7}}
            def invoke(args, **kwargs):
                self.assertEqual(args[:-1], ['talosctl', '--talosconfig', str(credential), '--endpoints', 'endpoint', '--nodes', 'node', 'etcd', 'snapshot'])
                self.assertEqual(kwargs['timeout'], 7)
                payload = b'x' * 512
                Path(args[-1]).write_bytes(payload + hashlib.sha256(payload).digest())
            with patch.object(snapshots.subprocess, 'run', side_effect=invoke):
                snapshots.produce(root, config)
            manifest = (root / 'talos-etcd' / 'manifest.json').read_text()
            self.assertIn('sha256', manifest)
            self.assertNotIn('synthetic-secret', manifest)
            for error in (subprocess.CalledProcessError(1, 'talosctl'), subprocess.TimeoutExpired('talosctl', 7), KeyboardInterrupt(), SystemExit(143)):
                with patch.object(snapshots.subprocess, 'run', side_effect=error):
                    with self.assertRaises(type(error)):
                        snapshots.produce(root, config)
                self.assertFalse((root / 'talos-etcd').exists())
                self.assertEqual(list((root / '.backup-client-staging').iterdir()), [])

    def test_producer_failure_stale_output_not_backed_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'sqlite').mkdir()
            (root / 'sqlite' / 'stale.db').write_bytes(b'old')
            (root / 'sqlite' / '.backup-client-managed').write_text('sqlite\n')
            config = {'sqlite': {'databases': [{'name': 'ha', 'path': str(root / 'missing')}]}}
            with patch.dict(os.environ, {'BACKUP_ROOT': tmp}), patch.object(client, 'load_config', return_value=config), patch.object(client, 'init_restic_repo') as init, patch.object(client.subprocess, 'check_call') as command:
                self.assertFalse(client.run_backup(True))
                init.assert_not_called()
                command.assert_not_called()
            self.assertFalse((root / 'sqlite').exists())
            self.assertEqual(list((root / '.backup-client-staging').iterdir()), [])

    def test_unmanaged_output_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'sqlite').mkdir()
            valuable = root / 'sqlite' / 'user-data'
            valuable.write_text('preserve')
            with self.assertRaises(ValueError):
                snapshots.produce(root, {'sqlite': {'databases': []}})
            self.assertEqual(valuable.read_text(), 'preserve')

    def test_orphan_staging_always_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staging = root / '.backup-client-staging'
            staging.mkdir()
            orphan = staging / 'sqlite-orphan'
            orphan.mkdir()
            (orphan / 'partial.db').write_bytes(b'partial')
            source = root / 'live.db'
            db = sqlite3.connect(source)
            db.execute('CREATE TABLE records(value)')
            db.close()
            config = {'sqlite': {'databases': [{'name': 'ha', 'path': str(source)}]}, 'init-repository': False}
            with patch.dict(os.environ, {'BACKUP_ROOT': tmp, 'BACKUP_HOSTNAME': 'test'}), patch.object(client, 'load_config', return_value=config), patch.object(client.subprocess, 'check_call') as command:
                self.assertTrue(client.run_backup(rotate=False))
                args = command.call_args.args[0]
                self.assertIn(str(staging), args)
                self.assertEqual(args[args.index(str(staging)) - 1], '--exclude')
            self.assertTrue(orphan.exists())

    def test_talos_rejects_invalid_payload_and_multiple_nodes(self):
        with tempfile.TemporaryDirectory() as tmp:
            credential = Path(tmp) / 'talosconfig'
            credential.write_text('synthetic')
            root = Path(tmp) / 'backup'
            root.mkdir()
            config = {'talos-etcd': {'talosconfig': str(credential), 'endpoints': ['endpoint'], 'nodes': ['node']}}
            for payload in (b'', b'truncated', b'x' * 512 + b'0' * 32):
                with patch.object(snapshots.subprocess, 'run', side_effect=lambda args, **kw: Path(args[-1]).write_bytes(payload)):
                    with self.assertRaises(ValueError):
                        snapshots.produce(root, config)
                self.assertFalse((root / 'talos-etcd').exists())
            config['talos-etcd']['nodes'] = ['node1', 'node2']
            with patch.object(snapshots.subprocess, 'run') as command:
                with self.assertRaises(ValueError):
                    snapshots.produce(root, config)
                command.assert_not_called()

    def test_backup_only_unlock_without_init_or_maintenance(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'BACKUP_ROOT': tmp, 'BACKUP_HOSTNAME': 'test'}), patch.object(client, 'load_config', return_value={'init-repository': False}), patch.object(client, 'init_restic_repo') as init, patch.object(client.subprocess, 'check_call') as command, patch.object(client, 'clean_old_backups') as rotate, patch.object(client, 'prune_repository') as prune:
            self.assertTrue(client.run_backup(True, rotate=False))
            init.assert_not_called()
            rotate.assert_not_called()
            prune.assert_not_called()
            self.assertEqual(command.call_count, 2)
            self.assertEqual(command.call_args_list[0].args[0], ['restic', 'unlock'])
            self.assertIn('backup', command.call_args_list[1].args[0])

    def test_duplicate_or_unsafe_sqlite_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'live.db'
            sqlite3.connect(path).close()
            for names in (['../bad'], ['same', 'same']):
                with self.assertRaises(ValueError):
                    snapshots.produce(tmp, {'sqlite': {'databases': [{'name': name, 'path': str(path)} for name in names]}})


if __name__ == '__main__':
    unittest.main()
