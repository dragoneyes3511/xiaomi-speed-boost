"""Real-filesystem tests for the deletion boundary and stale scan handling."""

import os
import socket
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from xiaomi_boost.cleaner import CacheRoot, ScanResult, cache_roots, clean, scan


class CleanerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.cache = self.base / ".cache"
        self.cache.mkdir()
        self.root = CacheRoot("test", self.cache)
        self.now = 2_000_000_000.0
        self.old = self.now - 10 * 86400

    def file(self, relative, data=b"cached", *, atime=None, mtime=None):
        path = self.cache / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        os.utime(path, (self.old if atime is None else atime, self.old if mtime is None else mtime))
        return path

    def snapshot(self, **kwargs):
        return scan([self.root], now=self.now, **kwargs)

    def test_only_old_files_are_selected_and_scan_never_deletes(self):
        old = self.file("nested/old")
        new = self.file("new", mtime=self.now)
        snapshot = self.snapshot()
        self.assertEqual([item.relative for item in snapshot.candidates], ["nested/old"])
        self.assertTrue(old.exists())
        self.assertTrue(new.exists())
        self.assertEqual(snapshot.selected_bytes, len(b"cached"))

    def test_recent_access_and_recent_modification_are_both_protected(self):
        accessed = self.file("accessed", atime=self.now)
        modified = self.file("modified", mtime=self.now)
        boundary = self.file("boundary", mtime=self.now - 7 * 86400)
        snapshot = self.snapshot()
        self.assertEqual(snapshot.candidates, [])
        self.assertEqual(clean(snapshot).removed_files, 0)
        self.assertTrue(all(path.exists() for path in (accessed, modified, boundary)))

    def test_unchanged_old_file_is_deleted_but_directories_remain(self):
        old = self.file("nested/old")
        result = clean(self.snapshot())
        self.assertEqual(result.removed_files, 1)
        self.assertEqual(result.removed_bytes, len(b"cached"))
        self.assertEqual(result.skipped_files, 0)
        self.assertFalse(old.exists())
        self.assertTrue(old.parent.is_dir())
        self.assertTrue(self.cache.is_dir())

    def test_symlink_files_and_directories_do_not_expose_external_files(self):
        outside = self.base / "outside"
        outside.mkdir()
        precious = outside / "precious"
        precious.write_bytes(b"private")
        os.utime(precious, (self.old, self.old))
        (self.cache / "file-link").symlink_to(precious)
        (self.cache / "directory-link").symlink_to(outside, target_is_directory=True)
        snapshot = self.snapshot()
        self.assertEqual(snapshot.candidates, [])
        clean(snapshot)
        self.assertEqual(precious.read_bytes(), b"private")
        self.assertTrue((self.cache / "file-link").is_symlink())

    def test_symlink_cache_root_is_rejected(self):
        old = self.file("old")
        alias = self.base / "cache-alias"
        alias.symlink_to(self.cache, target_is_directory=True)
        snapshot = scan([CacheRoot("alias", alias)], now=self.now)
        self.assertEqual(snapshot.candidates, [])
        self.assertTrue(snapshot.warnings)
        self.assertTrue(old.exists())

    def test_symlink_parent_of_cache_root_is_rejected(self):
        old = self.file("old")
        alias = self.base / "parent-alias"
        alias.symlink_to(self.base, target_is_directory=True)
        snapshot = scan([CacheRoot("alias", alias / ".cache")], now=self.now)
        self.assertEqual(snapshot.candidates, [])
        self.assertTrue(snapshot.warnings)
        self.assertTrue(old.exists())

    def test_hard_links_lockfiles_and_special_files_are_untouched(self):
        linked = self.file("linked")
        os.link(linked, self.base / "hard-link")
        locks = [self.file(name) for name in ("LOCK", ".lock", "lockfile", "db.lock", "db.lck", "process.pid", "SingletonCookie")]
        fifo = self.cache / "fifo"
        os.mkfifo(fifo)
        unix_socket = socket.socket(socket.AF_UNIX)
        self.addCleanup(unix_socket.close)
        unix_socket.bind(str(self.cache / "socket"))
        snapshot = self.snapshot()
        self.assertEqual(snapshot.candidates, [])
        self.assertTrue(linked.exists())
        self.assertTrue(all(path.exists() for path in locks))
        self.assertTrue(fifo.exists())

    def test_quota_limits_selected_bytes_and_counts_all_eligible_files(self):
        self.file("a", b"a" * 800)
        self.file("b", b"b" * 800)
        self.file("c", b"c" * 100)
        snapshot = self.snapshot(max_mib=1024 / (1024 * 1024))
        self.assertEqual(snapshot.eligible_files, 3)
        self.assertEqual(snapshot.eligible_bytes, 1700)
        self.assertEqual(snapshot.selected_bytes, 900)
        self.assertEqual([item.relative for item in snapshot.candidates], ["a", "c"])
        self.assertEqual(clean(snapshot).removed_files, 2)
        self.assertTrue((self.cache / "b").exists())

    def test_file_changed_after_scan_is_not_deleted(self):
        old = self.file("old")
        snapshot = self.snapshot()
        old.write_bytes(b"new application data")
        result = clean(snapshot)
        self.assertEqual(result.removed_files, 0)
        self.assertEqual(result.skipped_files, 1)
        self.assertEqual(old.read_bytes(), b"new application data")

    def test_file_accessed_after_scan_is_not_deleted(self):
        old = self.file("old")
        snapshot = self.snapshot()
        os.utime(old, (self.now, self.old))
        result = clean(snapshot)
        self.assertEqual(result.removed_files, 0)
        self.assertEqual(result.skipped_files, 1)
        self.assertTrue(old.exists())

    def test_file_replaced_after_scan_is_not_deleted(self):
        old = self.file("old")
        snapshot = self.snapshot()
        replacement = self.file("replacement", b"replacement")
        os.replace(replacement, old)
        result = clean(snapshot)
        self.assertEqual(result.removed_files, 0)
        self.assertEqual(result.skipped_files, 1)
        self.assertEqual(old.read_bytes(), b"replacement")

    def test_file_replaced_by_symlink_after_scan_is_not_unlinked(self):
        old = self.file("old")
        precious = self.base / "precious"
        precious.write_bytes(b"private")
        snapshot = self.snapshot()
        old.unlink()
        old.symlink_to(precious)
        result = clean(snapshot)
        self.assertEqual(result.removed_files, 0)
        self.assertEqual(result.skipped_files, 1)
        self.assertTrue(old.is_symlink())
        self.assertEqual(precious.read_bytes(), b"private")

    def test_parent_swapped_for_external_symlink_is_rejected(self):
        self.file("nested/old")
        outside = self.base / "outside"
        outside.mkdir()
        precious = outside / "old"
        precious.write_bytes(b"private")
        snapshot = self.snapshot()
        (self.cache / "nested").rename(self.cache / "original")
        (self.cache / "nested").symlink_to(outside, target_is_directory=True)
        result = clean(snapshot)
        self.assertEqual(result.removed_files, 0)
        self.assertEqual(result.skipped_files, 1)
        self.assertEqual(precious.read_bytes(), b"private")
        self.assertTrue((self.cache / "original" / "old").exists())

    def test_root_replaced_by_new_directory_is_rejected(self):
        self.file("old")
        snapshot = self.snapshot()
        self.cache.rename(self.base / "original-cache")
        self.cache.mkdir()
        replacement = self.file("old", b"new data")
        result = clean(snapshot)
        self.assertEqual(result.removed_files, 0)
        self.assertEqual(result.skipped_files, 1)
        self.assertEqual(replacement.read_bytes(), b"new data")
        self.assertTrue((self.base / "original-cache" / "old").exists())

    def test_repeated_cleanup_and_vanished_candidates_are_safe(self):
        old = self.file("old")
        snapshot = self.snapshot()
        first = clean(snapshot)
        second = clean(snapshot)
        self.assertEqual(first.removed_files, 1)
        self.assertEqual(second.removed_files, 0)
        self.assertEqual(second.skipped_files, 1)
        self.assertFalse(old.exists())
        self.assertEqual(self.snapshot().candidates, [])

    def test_missing_cache_is_a_valid_empty_scan(self):
        snapshot = scan([CacheRoot("missing", self.base / "missing")], now=self.now)
        self.assertEqual(snapshot.candidates, [])
        self.assertEqual(snapshot.warnings, [])

    def test_unsafe_relative_candidate_paths_are_rejected(self):
        self.file("old")
        precious = self.base / "precious"
        precious.write_bytes(b"private")
        candidate = self.snapshot().candidates[0]
        for relative in ("../precious", str(precious), "nested/../../precious", ""):
            with self.subTest(relative=relative):
                result = clean(ScanResult(candidates=[replace(candidate, relative=relative)]))
                self.assertEqual(result.removed_files, 0)
                self.assertEqual(result.skipped_files, 1)
        self.assertEqual(precious.read_bytes(), b"private")

    def test_invalid_thresholds_cannot_bypass_safety_limits(self):
        for days in (0, -1, float("nan"), float("inf")):
            with self.subTest(days=days), self.assertRaises(ValueError):
                self.snapshot(days=days)
        for cap in (0, -1, 4097, float("nan"), float("inf")):
            with self.subTest(max_mib=cap), self.assertRaises(ValueError):
                self.snapshot(max_mib=cap)

    def test_scan_limit_stops_with_an_explicit_warning(self):
        self.file("a")
        self.file("b")
        with mock.patch("xiaomi_boost.cleaner.MAX_SCAN_FILES", 1):
            snapshot = self.snapshot()
        self.assertEqual(snapshot.examined_files, 1)
        self.assertEqual(len(snapshot.candidates), 1)
        self.assertTrue(snapshot.warnings)

    def test_environment_cannot_add_an_arbitrary_storage_target(self):
        for prefix in ("/sdcard", "/storage/emulated/0/Download", "/tmp/custom/usr", ""):
            with self.subTest(prefix=prefix), mock.patch.dict(os.environ, {"PREFIX": prefix}), mock.patch("pathlib.Path.home", return_value=self.base):
                roots = cache_roots()
                self.assertEqual(roots, [CacheRoot("home-cache", self.base / ".cache")])


if __name__ == "__main__":
    unittest.main()
