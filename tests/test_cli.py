"""Exercise actual CLI processes against a disposable home, never real caches."""

import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from xiaomi_boost import cli


REPO = Path(__file__).resolve().parents[1]


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.cache = self.home / ".cache"
        self.cache.mkdir()
        self.environment = dict(os.environ, HOME=str(self.home), PREFIX="")

    def old_file(self, name="old.bin", data=b"cached"):
        path = self.cache / name
        path.write_bytes(data)
        old = time.time() - 10 * 86400
        os.utime(path, (old, old))
        return path

    def command(self, *arguments):
        return subprocess.run(
            [sys.executable, str(REPO / "boost.py"), *arguments],
            cwd=self.home, env=self.environment, capture_output=True,
            text=True, timeout=10,
        )

    def test_scan_and_default_clean_leave_files_unchanged(self):
        old = self.old_file()
        for command in ("scan", "clean"):
            with self.subTest(command=command):
                completed = self.command(command)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertIn("old.bin", completed.stdout)
                self.assertIn("ดูตัวอย่างเท่านั้น", completed.stdout)
                self.assertTrue(old.exists())

    def test_apply_cleans_old_cache_but_preserves_personal_and_recent_files(self):
        old = self.old_file()
        recent = self.cache / "new.bin"
        recent.write_bytes(b"new")
        personal = self.home / "save-game.dat"
        personal.write_bytes(b"save game")
        link = self.cache / "shortcut"
        link.symlink_to(personal)
        completed = self.command("clean", "--apply")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertFalse(old.exists())
        self.assertEqual(recent.read_bytes(), b"new")
        self.assertEqual(personal.read_bytes(), b"save game")
        self.assertTrue(link.is_symlink())
        repeated = self.command("clean", "--apply")
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertIn("ลบแล้ว 0 ไฟล์", repeated.stdout)

    def test_invalid_numbers_fail_without_deleting(self):
        old = self.old_file()
        for option, value in (("--days", "0"), ("--days", "nan"), ("--max-mib", "inf"), ("--max-mib", "4097")):
            with self.subTest(option=option, value=value):
                completed = self.command("clean", "--apply", option, value)
                self.assertEqual(completed.returncode, 2)
                self.assertTrue(old.exists())

    def test_no_command_with_noninteractive_input_prints_help(self):
        completed = self.command()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("usage:", completed.stdout)

    def test_auto_rescans_each_cycle_and_stops_on_interrupt(self):
        first = self.old_file("first")
        second = self.cache / "second"
        sleeps = 0

        def after_cycle(interval):
            nonlocal sleeps
            self.assertEqual(interval, 60)
            sleeps += 1
            if sleeps == 1:
                self.assertFalse(first.exists())
                self.old_file("second")
            else:
                raise KeyboardInterrupt

        with patch.dict(os.environ, self.environment, clear=True), \
                patch.object(cli.time, "sleep", side_effect=after_cycle), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(["auto", "--interval", "60", "--apply"])
        self.assertEqual(code, 130)
        self.assertEqual(sleeps, 2)
        self.assertFalse(second.exists())

    def test_auto_preview_does_not_delete_on_multiple_cycles(self):
        old = self.old_file()
        with patch.dict(os.environ, self.environment, clear=True), \
                patch.object(cli.time, "sleep", side_effect=[None, KeyboardInterrupt]), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(["auto", "--interval", "60"])
        self.assertEqual(code, 130)
        self.assertTrue(old.exists())

    def test_status_runs_without_android(self):
        completed = self.command("status")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("RAM", completed.stdout)
        self.assertIn("พื้นที่ว่าง", completed.stdout)

    def test_game_rejects_shell_syntax(self):
        marker = self.home / "injected"
        completed = self.command("game", f"com.game; touch {marker}")
        self.assertEqual(completed.returncode, 1)
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
