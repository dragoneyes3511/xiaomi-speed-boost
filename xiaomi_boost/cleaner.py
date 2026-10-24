"""Age-limited cache cleanup, confined to private cache roots.

Scanning and removal use directory descriptors and never follow symlinks. A
preview is a snapshot: every file and its root are checked again before unlink.
No directory, hard link, lock file, or non-regular file is ever removed.
"""

from __future__ import annotations

import math
import os
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path


DEFAULT_DAYS = 7.0
DEFAULT_MAX_MIB = 256.0
MAX_SCAN_FILES = 50_000
MAX_SCAN_DIRECTORIES = 10_000
MAX_DEPTH = 64
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


@dataclass(frozen=True)
class CacheRoot:
    name: str
    path: Path


@dataclass(frozen=True)
class Candidate:
    root: CacheRoot
    root_identity: tuple[int, int]
    relative: str
    fingerprint: tuple[int, ...]
    size: int

    @property
    def path(self) -> Path:
        return self.root.path / self.relative


@dataclass
class ScanResult:
    candidates: list[Candidate] = field(default_factory=list)
    eligible_files: int = 0
    eligible_bytes: int = 0
    examined_files: int = 0
    examined_directories: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def selected_bytes(self) -> int:
        return sum(item.size for item in self.candidates)


@dataclass
class CleanResult:
    removed_files: int = 0
    removed_bytes: int = 0
    skipped_files: int = 0
    warnings: list[str] = field(default_factory=list)


def cache_roots() -> list[CacheRoot]:
    """Fixed private roots only; never accept arbitrary cleanup paths from CLI."""
    home = Path.home().resolve()
    roots = [CacheRoot("home-cache", home / ".cache")]
    prefix = os.environ.get("PREFIX", "")
    if prefix in (
        "/data/data/com.termux/files/usr",
        "/data/user/0/com.termux/files/usr",
    ):
        roots.append(CacheRoot("termux-cache", Path(prefix).parents[1].resolve() / "cache"))
    return roots


def _fingerprint(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
        info.st_atime_ns, info.st_ctime_ns, info.st_nlink,
    )


def _is_lock(name: str) -> bool:
    lower = name.lower()
    return (
        lower in {"lock", ".lock", "lockfile"}
        or lower.endswith((".lock", ".lck", ".pid"))
        or lower.startswith("singleton")
    )


def _open_root(path: Path) -> int:
    """Reject symlinks in every component, including parents of the root."""
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("cache root must be an absolute path without '..'")
    # Android's /data may be searchable but not readable by the Termux UID.
    # O_PATH needs only traversal permission for intermediate directories.
    traversal_flags = getattr(os, "O_PATH", os.O_RDONLY) | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open("/", traversal_flags)
    try:
        for index, part in enumerate(path.parts[1:], start=1):
            flags = _DIRECTORY_FLAGS if index == len(path.parts) - 1 else traversal_flags
            following = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = following
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def scan(
    roots: list[CacheRoot] | None = None,
    *,
    days: float = DEFAULT_DAYS,
    max_mib: float = DEFAULT_MAX_MIB,
    now: float | None = None,
) -> ScanResult:
    if not math.isfinite(days) or days < 1:
        raise ValueError("days must be finite and at least 1")
    if not math.isfinite(max_mib) or not 0 < max_mib <= 4096:
        raise ValueError("max_mib must be greater than 0 and at most 4096")
    deadline = (time.time() if now is None else now) - days * 86400
    limit = int(max_mib * 1024 * 1024)
    selected_bytes = 0
    result = ScanResult()

    def warn(message: str) -> None:
        if len(result.warnings) < 20:
            result.warnings.append(message)

    for root in cache_roots() if roots is None else roots:
        try:
            root_fd = _open_root(root.path)
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as error:
            warn(f"{root.name}: {error}")
            continue
        try:
            root_stat = os.fstat(root_fd)
            identity = (root_stat.st_dev, root_stat.st_ino)
            walker = os.fwalk(
                ".", topdown=True, follow_symlinks=False, dir_fd=root_fd,
                onerror=lambda error: warn(str(error)),
            )
            try:
                for directory, subdirs, files, directory_fd in walker:
                    if result.examined_directories >= MAX_SCAN_DIRECTORIES:
                        warn(f"Scan stopped at {MAX_SCAN_DIRECTORIES} directories.")
                        return result
                    result.examined_directories += 1
                    # Never cross filesystems; leave symlink directories untouched.
                    accepted = []
                    if len(Path(directory).parts) >= MAX_DEPTH and subdirs:
                        warn(f"Skipped directories deeper than {MAX_DEPTH} levels.")
                        subdirs[:] = []
                    for name in sorted(subdirs):
                        try:
                            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                            if stat.S_ISDIR(info.st_mode) and info.st_dev == root_stat.st_dev:
                                accepted.append(name)
                        except OSError as error:
                            warn(str(error))
                    subdirs[:] = accepted
                    for name in sorted(files):
                        if result.examined_files >= MAX_SCAN_FILES:
                            warn(f"Scan stopped at {MAX_SCAN_FILES} files; narrow your cache manually.")
                            return result
                        result.examined_files += 1
                        if _is_lock(name):
                            continue
                        try:
                            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                        except OSError as error:
                            warn(str(error))
                            continue
                        if (
                            not stat.S_ISREG(info.st_mode)
                            or info.st_nlink != 1
                            or info.st_dev != root_stat.st_dev
                            or max(info.st_mtime, info.st_atime) >= deadline
                        ):
                            continue
                        result.eligible_files += 1
                        result.eligible_bytes += info.st_size
                        if selected_bytes + info.st_size <= limit:
                            relative = str(Path(directory) / name)
                            result.candidates.append(Candidate(root, identity, relative, _fingerprint(info), info.st_size))
                            selected_bytes += info.st_size
            finally:
                walker.close()
        finally:
            os.close(root_fd)
    return result


def clean(snapshot: ScanResult) -> CleanResult:
    """Unlink unchanged candidates only. Callers must explicitly opt in."""
    result = CleanResult()
    for item in snapshot.candidates:
        descriptor = None
        try:
            parts = Path(item.relative).parts
            if not parts or Path(item.relative).is_absolute() or any(p in {".", ".."} for p in parts):
                raise ValueError("unsafe relative path")
            descriptor = _open_root(item.root.path)
            info = os.fstat(descriptor)
            if (info.st_dev, info.st_ino) != item.root_identity:
                raise ValueError("cache root changed after scan")
            for part in parts[:-1]:
                following = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = following
                if os.fstat(descriptor).st_dev != item.root_identity[0]:
                    raise ValueError("filesystem changed after scan")
            info = os.stat(parts[-1], dir_fd=descriptor, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or _fingerprint(info) != item.fingerprint:
                raise ValueError("file changed or was accessed after scan")
            os.unlink(parts[-1], dir_fd=descriptor)
            result.removed_files += 1
            result.removed_bytes += item.size
        except (OSError, ValueError) as error:
            result.skipped_files += 1
            if len(result.warnings) < 20:
                result.warnings.append(f"{item.path}: {error}")
        finally:
            if descriptor is not None:
                os.close(descriptor)
    return result
