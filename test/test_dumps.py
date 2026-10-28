"""Dump regressions; no database servers or external dump tools required."""
import gzip
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mysqldump
import pgdump
import mongodump
import elasticdump


class DumpTests(unittest.TestCase):
    def test_both_pipeline_statuses_and_atomic_output(self):
        for module in (mysqldump, pgdump):
            for producer_status, gzip_status in ((0, 0), (7, 0), (0, 9), (7, 9)):
                with self.subTest(module=module.__name__, producer=producer_status, gzip=gzip_status):
                    with tempfile.TemporaryDirectory() as directory:
                        output = Path(directory) / 'dump with spaces.gz'
                        output.write_bytes(b'previous backup')
                        producer = mock.Mock()
                        compressor = mock.Mock()
                        producer.wait.return_value = producer_status
                        compressor.wait.return_value = gzip_status
                        with mock.patch.object(module.subprocess, 'Popen', side_effect=[producer, compressor]) as popen:
                            if producer_status or gzip_status:
                                with self.assertRaises(subprocess.CalledProcessError):
                                    module._dump_gzip(['dump', 'db; echo unsafe'], str(output), {})
                                self.assertEqual(output.read_bytes(), b'previous backup')
                            else:
                                module._dump_gzip(['dump', 'db; echo unsafe'], str(output), {})
                            self.assertEqual(popen.call_args_list[0].args[0], ['dump', 'db; echo unsafe'])
                            self.assertNotIn('shell', popen.call_args_list[0].kwargs)
                        producer.wait.assert_called_once()
                        compressor.wait.assert_called_once()
                        self.assertEqual(list(Path(directory).iterdir()), [output])

    def test_gzip_start_failure_reaps_producer(self):
        for module in (mysqldump, pgdump):
            with tempfile.TemporaryDirectory() as directory:
                producer = mock.Mock()
                producer.poll.return_value = None
                with mock.patch.object(module.subprocess, 'Popen', side_effect=[producer, OSError('missing gzip')]):
                    with self.assertRaises(OSError):
                        module._dump_gzip(['dump'], os.path.join(directory, 'dump.gz'), {})
                producer.stdout.close.assert_called_once()
                producer.terminate.assert_called_once()
                producer.wait.assert_called_once()
                self.assertEqual(os.listdir(directory), [])

    def test_mysql_options_are_separate_arguments(self):
        with mock.patch.object(mysqldump, 'mysql_list_database', return_value=['db name; echo unsafe']), \
             mock.patch.object(mysqldump, '_dump_gzip') as dump:
            self.assertTrue(mysqldump.mysql_dump_with_config('/tmp', {
                'host': 'host', 'username': 'user', 'password': 'secret',
                'mysqldump-extra-args': ['--single-transaction', '--ignore-table=db.table']}))
            self.assertEqual(dump.call_count, 2)
            for call in dump.call_args_list:
                self.assertIn('--single-transaction', call.args[0])
                self.assertIn('--ignore-table=db.table', call.args[0])
                self.assertEqual(call.args[0][-1], 'db name; echo unsafe')

    def test_postgres_listing_is_checked_and_keeps_spaces(self):
        with mock.patch.object(pgdump.subprocess, 'check_output', return_value=b' db with spaces\npostgres\n') as check:
            self.assertEqual(pgdump.pg_list_database('host', 5432, 'user', 'pw'), ['db with spaces', 'postgres'])
            self.assertIn('--tuples-only', check.call_args.args[0])
            self.assertNotIn('shell', check.call_args.kwargs)
        with mock.patch.object(pgdump.subprocess, 'check_output', side_effect=subprocess.CalledProcessError(1, 'psql')):
            self.assertFalse(pgdump.pg_dump('/tmp', 'host', 5432, 'user', 'pw', None, None))

    def test_mongo_argv_and_failure(self):
        for error in (subprocess.CalledProcessError(1, 'mongodump'), FileNotFoundError()):
            with mock.patch.object(mongodump.subprocess, 'check_call', side_effect=error) as check:
                self.assertFalse(mongodump.mongodump('/tmp/with space', 'host', 27017, 'user', 'pw; unsafe', 4))
                self.assertIn('--password=pw; unsafe', check.call_args.args[0])
                self.assertIn('mongodump', check.call_args.args[0])
                self.assertNotIn('shell', check.call_args.kwargs)

    def test_elastic_partial_output_is_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            def fail(command):
                Path(command[-1]).write_text('partial')
                raise subprocess.CalledProcessError(2, 'elasticdump')
            with mock.patch.object(elasticdump, 'es_list_indices', return_value=['index']), \
                 mock.patch.object(elasticdump.subprocess, 'check_call', side_effect=fail):
                self.assertFalse(elasticdump.es_dump(directory, 'http://host', None, None, None, None))
            self.assertEqual(os.listdir(directory), [])


if __name__ == '__main__':
    unittest.main()
