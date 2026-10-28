import os
import subprocess
import tempfile
import unittest
from datetime import timedelta
from unittest.mock import patch

import backup_client as client


class BackupClientTests(unittest.TestCase):
    def test_timeout_is_optional(self):
        for value in (None, "0s", ""):
            env = {} if value is None else {"RESTIC_PRUNE_TIMEOUT": value}
            with self.subTest(value=value), patch.dict(os.environ, env, clear=True):
                self.assertIsNone(client.get_prune_timeout())

    def test_timeout_parsing(self):
        with patch.dict(os.environ, {"RESTIC_PRUNE_TIMEOUT": "1d2h3m4s"}, clear=True):
            self.assertEqual(client.get_prune_timeout(), timedelta(days=1, hours=2, minutes=3, seconds=4))
        with patch.dict(os.environ, {"RESTIC_PRUNE_TIMEOUT": "invalid"}, clear=True):
            with self.assertRaises(SystemExit):
                client.get_prune_timeout()

    def test_flags_defaults_and_boolean_values(self):
        for setting in (None, False, True):
            config = {} if setting is None else {"exclude-caches": setting, "ignore-inode": setting}
            with self.subTest(setting=setting), tempfile.TemporaryDirectory() as root:
                env = {"BACKUP_ROOT": root, "BACKUP_HOSTNAME": "test"}
                with patch.dict(os.environ, env), patch.object(client, "init_restic_repo", return_value=True), patch.object(client, "load_config", return_value=config), patch.object(client, "clean_old_backups", return_value=True), patch.object(client.subprocess, "check_call") as call:
                    self.assertTrue(client.run_backup())
                    args = next(c.args[0] for c in call.call_args_list if "backup" in c.args[0])
                    for flag in ("--exclude-caches", "--ignore-inode"):
                        self.assertEqual(flag in args, setting is not False)

    def test_failed_init_aborts_before_backup(self):
        with patch.dict(os.environ, {"BACKUP_ROOT": "/unused"}), patch.object(client, "init_restic_repo", return_value=False), patch.object(client.subprocess, "check_call") as call:
            self.assertFalse(client.run_backup())
            call.assert_not_called()

    def test_init_success_and_existing(self):
        for error in (None, subprocess.CalledProcessError(1, "restic", output=b"config file already exists")):
            with patch.object(client.subprocess, "check_output", side_effect=error):
                self.assertTrue(client.init_restic_repo())

    def test_no_retention_is_successful_noop(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(client.subprocess, "check_call") as call:
            self.assertTrue(client.clean_old_backups({}))
            call.assert_not_called()

    def test_partial_backup_never_rotates(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"BACKUP_ROOT": root, "BACKUP_HOSTNAME": "test"}), patch.object(client, "init_restic_repo", return_value=True), patch.object(client, "load_config", return_value={}), patch.object(client, "clean_old_backups") as rotate:
            def invoke(args, **kwargs):
                if "backup" in args:
                    raise subprocess.CalledProcessError(3, args)
            with patch.object(client.subprocess, "check_call", side_effect=invoke):
                self.assertFalse(client.run_backup())
                rotate.assert_not_called()

    def test_empty_config_file(self):
        with tempfile.NamedTemporaryFile() as config, patch.dict(os.environ, {"BACKUP_CONFIG": config.name}):
            self.assertEqual(client.load_config(), {})


if __name__ == "__main__":
    unittest.main()
